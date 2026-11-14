"""Bounded candidate queue observation; no exact-run isolation or rollout claim."""

import argparse
import json
import re
import signal
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any, Callable, ContextManager, Iterator, cast
from urllib.parse import quote

from . import manager as _manager
from . import model as _model
from . import state as _state
from .backend import ROOT, Backend, digest, execute, helper, image_binding
from .identity import binding
from .manager import Manager
from .model import ROOT as SOURCE

# Existing fleet APIs exchange dynamic JSON records. New campaign boundaries
# remain typed without pretending those legacy adapters have static schemas.
State: Any = _state.State
config: Any = _model.config
gh: Any = _manager.gh
pages: Any = _manager.pages
trusted: Any = _manager.trusted


def validate_plan(p: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "repositories",
        "acknowledge_repository_queue",
        "admission_seconds",
        "cleanup_seconds",
        "image_path",
        "image_sha256",
        "image_source_sha",
        "controller_sha256",
        "recipe_sha256",
        "tool_closure_sha256",
        "execution_source_sha",
        "execution_controller_sha256",
    }
    if not isinstance(p, dict) or set(p) != fields:
        raise ValueError("Unexpected or missing plan fields")
    expected = [cfg["owner"] + "/" + r for r in cfg["repositories"]]
    if p["repositories"] != expected or p["acknowledge_repository_queue"] is not True:
        raise ValueError(
            "Explicit eight-repository queue scope acknowledgment required"
        )
    if cfg["hosts"]["strix"]["capacity"] != 2 or cfg["job_timeout_seconds"] != 900:
        raise ValueError("Existing two-slot/15-minute policy changed")
    for key, limit in (("admission_seconds", 1200), ("cleanup_seconds", 300)):
        if type(p[key]) is not int or not 1 <= p[key] <= limit:
            raise ValueError("Invalid bounded duration")
    for key in (
        "image_sha256",
        "controller_sha256",
        "recipe_sha256",
        "tool_closure_sha256",
        "image_source_sha",
        "execution_source_sha",
        "execution_controller_sha256",
    ):
        width = 40 if key in ("image_source_sha", "execution_source_sha") else 64
        if not isinstance(p[key], str) or not re.fullmatch(
            r"[0-9a-f]{%d}" % width, p[key]
        ):
            raise ValueError("Invalid exact candidate binding")
    image = Path(p["image_path"])
    if image.parent != ROOT / "images" or not re.fullmatch(
        r"candidate-[0-9a-f]{12}\.qcow2", image.name
    ):
        raise ValueError("Only a sealed candidate path is allowed")
    return p


def verify_image(p: dict[str, Any], directory: str | Path) -> None:
    image = Path(p["image_path"])
    if image.is_symlink() or not image.is_file():
        raise ValueError("Candidate missing or symlinked")
    stat = image.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o222:
        raise ValueError("Candidate must be root-owned and read-only")
    sha = digest(image)
    if sha != p["image_sha256"]:
        raise ValueError("Wrong image digest")
    bound = image_binding(
        image, Path(directory) / "candidate.json", sha, candidate=True
    )
    if any(
        bound[k] != p[k]
        for k in (
            "image_source_sha",
            "controller_sha256",
            "recipe_sha256",
            "tool_closure_sha256",
        )
    ):
        raise ValueError("Wrong candidate source binding")


def verify_executor(plan: dict[str, Any]) -> dict[str, str]:
    revision = execute(["git", "-C", str(SOURCE), "rev-parse", "HEAD"]).stdout.strip()
    dirty = execute(
        ["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=all"]
    ).stdout.strip()
    controller = binding(SOURCE)["controller_sha256"]
    if (
        dirty
        or revision != plan["execution_source_sha"]
        or controller != plan["execution_controller_sha256"]
    ):
        raise ValueError("Execution controller differs from approved clean source")
    return {"source_sha": revision, "controller_sha256": controller}


class CampaignDeadline(BaseException):
    """Bypasses broad operational Exception handlers; capacity remains occupied."""


@contextmanager
def time_budget(seconds: float) -> Iterator[None]:
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("Existing process timer; refusing to replace it")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum: int, frame: FrameType | None) -> None:
        raise CampaignDeadline("Campaign phase deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def protected_images() -> dict[str, str]:
    """Never write these files; verify both pairs before/after the campaign."""
    return {
        name: digest(ROOT / "images" / name)
        for name in (
            "qualified.qcow2",
            "qualified.json",
            "qualified.previous.qcow2",
            "qualified.previous.json",
        )
    }


def idle_host(manager: "QueueManager") -> dict[str, str]:
    service = execute(
        [
            "systemctl",
            "show",
            "runspindle-fleet.service",
            "--property=ActiveState,SubState,UnitFileState",
        ],
        check=True,
    )
    fields = dict(
        line.split("=", 1) for line in service.stdout.splitlines() if "=" in line
    )
    if fields.get("ActiveState") != "inactive":
        raise RuntimeError("Existing manager must already be stopped")
    if (
        manager.state.get("drained") is not True
        or manager.state.active()
        or manager.state.get("error")
    ):
        raise RuntimeError("Shared state must be explicitly drained and idle")
    for slot in range(2):
        guest = helper("observe", slot)
        if (
            guest.get("ActiveState") not in ("inactive", "failed")
            or int(guest.get("MainPID", -1)) != 0
        ):
            raise RuntimeError("Guest ownership or termination not verified")
        if any((ROOT / "slots" / str(slot)).iterdir()):
            raise RuntimeError("Existing slot files require reconciliation")
    for repo in manager.cfg["repositories"]:
        if any(
            r["name"].startswith("runspindle-fleet-")
            for r in cast(Callable[..., list[dict[str, Any]]], manager.registrations)(
                repo
            )
        ):
            raise RuntimeError("Existing fleet registrations require reconciliation")
    return fields


class CandidateBackend(Backend):
    def __init__(self, state: Any, plan: dict[str, Any], campaign_id: str) -> None:
        cast(Callable[..., None], super().__init__)(state, Path(plan["image_path"]))
        self.plan, self.campaign_id = plan, campaign_id

    def create(self, resource: dict[str, Any], prepare: bool = False) -> None:
        if prepare:
            raise ValueError("Candidate backend cannot prepare an image")
        verify_executor(self.plan)
        verify_image(self.plan, self.state.directory)
        cast(Callable[..., None], super().create)(resource)


class QueueManager(Manager):
    """Preserve provisioning trust; observe broader actual server assignment."""

    backend: CandidateBackend
    state: Any
    arm_deadline: Callable[[], None]

    def __init__(
        self,
        cfg: dict[str, Any],
        directory: str | Path,
        backend: CandidateBackend,
        admission_end: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cast(Callable[..., None], super().__init__)(cfg, directory, backend)
        self.admission_end, self.clock = admission_end, clock
        self.assignment_watch: dict[str, set[tuple[int, int]]] = {
            repo: set() for repo in cfg["repositories"]
        }
        self.arm_deadline = lambda: None

    def reserve_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self.state.reserve(
                job,
                details={
                    "candidate_campaign": self.backend.campaign_id,
                    "candidate_sha256": self.backend.plan["image_sha256"],
                },
            ),
        )

    def can_admit(self) -> bool:
        return (
            not self.stopping
            and self.clock() < self.admission_end
            and not self.state.get("error")
            and not any(
                r["phase"] in ("uncertain", "cleaning") for r in self.state.active()
            )
        )

    def inventory(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        # Inventory all eligible-server observations, including untrusted runs,
        # so actual claims cannot silently inherit a selected job's identity.
        queue, jobs = [], []
        for repo in self.cfg["repositories"]:
            full = self.cfg["owner"] + "/" + repo
            runs = {}
            for status in ("queued", "in_progress"):
                for run in pages(
                    "repos/" + full + "/actions/runs?status=" + status, "workflow_runs"
                ):
                    runs[(run["id"], run["run_attempt"])] = run
            active = [r for r in self.state.active() if r["repository"] == repo]
            if active:
                since = datetime.fromtimestamp(
                    min(r["created"] for r in active) - 60, timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                for run in pages(
                    "repos/"
                    + full
                    + "/actions/runs?created="
                    + quote(">" + since, safe=""),
                    "workflow_runs",
                ):
                    runs[(run["id"], run["run_attempt"])] = run
            watched = set(self.assignment_watch[repo])
            for resource in active:
                claim = resource["details"].get("claimed_job", {})
                if claim.get("run_id") and claim.get("run_attempt"):
                    watched.add((claim["run_id"], claim["run_attempt"]))
            for identity, attempt in watched - set(runs):
                run = gh(
                    "repos/"
                    + full
                    + "/actions/runs/"
                    + str(identity)
                    + "/attempts/"
                    + str(attempt)
                )
                if run.get("id") != identity or run.get("run_attempt") != attempt:
                    raise RuntimeError("Watched run attempt cannot be resolved")
                runs[(identity, attempt)] = run
            self.assignment_watch[repo] = {
                (r["id"], r["run_attempt"])
                for r in runs.values()
                if r["status"] != "completed"
            }
            for run in runs.values():
                for job in pages(
                    "repos/"
                    + full
                    + "/actions/runs/"
                    + str(run["id"])
                    + "/attempts/"
                    + str(run["run_attempt"])
                    + "/jobs",
                    "jobs",
                ):
                    observed = job | {
                        "repository": repo,
                        "run_id": run["id"],
                        "run_attempt": run["run_attempt"],
                        "head_sha": run["head_sha"],
                        "event": run["event"],
                        "head_branch": run["head_branch"],
                        "workflow_path": run.get("path"),
                        "source_trusted": trusted(run, full),
                    }
                    jobs.append(observed)
                    if observed["status"] == "queued" and observed["source_trusted"]:
                        queue.append(observed)
        queue.sort(key=lambda j: (j.get("created_at", ""), j["id"]))
        return queue, jobs

    def cancel_claim(self, resource: dict[str, Any], job: dict[str, Any]) -> bool:
        # GitHub offers run-scoped cancellation, not atomic attempt-scoped
        # cancellation. Never risk cancelling a successor rerun via this API.
        run = gh(
            "repos/"
            + self.cfg["owner"]
            + "/"
            + resource["repository"]
            + "/actions/runs/"
            + str(job["run_id"])
        )
        exact = (
            run.get("id") == job["run_id"]
            and run.get("head_sha") == job["head_sha"]
            and run.get("run_attempt") == job["run_attempt"]
        )
        self.state.update(
            resource,
            resource["phase"],
            remote_cancellation="not_requested_guest_cleanup_only",
            cancellation_identity_verified=exact,
        )
        if not exact:
            raise RuntimeError("Claimed run attempt changed; no remote cancellation")
        return False

    def advance(self, resource: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        if resource["phase"] == "starting" and not resource["details"].get(
            "candidate_job_deadline"
        ):
            # Conservative: starts before registration/isolation, not after a job
            # starts. The independent alarm can end the campaign early, never late.
            self.state.update(
                resource, "starting", candidate_job_deadline=time.time() + 900
            )
        self.arm_deadline()
        claims = [j for j in jobs if j.get("runner_name") == resource["runner_name"]]
        if len(claims) > 1:
            self.state.update(resource, "uncertain", error="Multiple server claims")
            self.stopping = True
            return
        if claims:
            # This is attribution after assignment, NOT a pre-execution gate.
            self.state.update(resource, resource["phase"], claimed_job=claims[0])
            if not claims[0].get("source_trusted"):
                self.state.update(
                    resource, "uncertain", error="Untrusted server assignment"
                )
                self.stopping = True
                return
        cast(Callable[..., None], super().advance)(resource, jobs)
        self.arm_deadline()


def cleanup_campaign(manager: QueueManager, campaign_id: str) -> None:
    """Only own reservations; stop and deregister independently on any failure."""
    for resource in manager.state.active():
        if resource["details"].get("candidate_campaign") != campaign_id:
            continue  # Never touch foreign resources; final idle verification stays blocked.
        manager.state.update(resource, "cleaning")
        errors = []
        claimed = resource["details"].get("claimed_job", {})
        if claimed and claimed.get("status") != "completed":
            try:
                manager.cancel_claim(resource, claimed)
            except Exception:
                errors.append("claimed run identity unresolved; no remote cancellation")
        try:
            cast(Callable[..., None], manager.backend.destroy)(resource)
        except Exception:
            errors.append("guest termination or files uncertain")
        try:
            cast(Callable[..., None], manager.remove_registration)(resource)
        except Exception:
            errors.append("runner deregistration uncertain")
        if errors:
            manager.state.update(resource, "uncertain", error="; ".join(errors))
        else:
            manager.state.update(
                resource,
                "destroyed",
                cleanup_verified=True,
                operational_status="blocked",
            )


def receipts(state: Any, campaign_id: str) -> list[dict[str, Any]]:
    rows = []
    for raw in state.db.execute("SELECT * FROM resources ORDER BY created"):
        row = dict(raw)
        details = json.loads(row["details"])
        if details.get("candidate_campaign") != campaign_id:
            continue
        claimed = details.get("claimed_job", {})
        rows.append(
            {
                "resource_id": row["id"],
                "repository": row["repository"],
                "requested_job": row["requested_job"],
                "phase": row["phase"],
                "candidate_sha256": details.get("candidate_sha256"),
                "cleanup_verified": details.get("cleanup_verified", False),
                "actual_claim": {
                    k: claimed.get(k)
                    for k in (
                        "id",
                        "run_id",
                        "run_attempt",
                        "head_sha",
                        "workflow_path",
                        "event",
                        "head_branch",
                        "source_trusted",
                        "conclusion",
                    )
                },
                "operational_status": details.get("operational_status", "blocked"),
                "remote_cancellation": details.get(
                    "remote_cancellation", "not_requested"
                ),
            }
        )
    return rows


def run_campaign(
    plan: dict[str, Any],
    directory: str | Path,
    acknowledge: bool,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    budget: Callable[[float], ContextManager[None]] = time_budget,
) -> dict[str, Any]:
    if acknowledge is not True:
        raise ValueError(
            "Explicit execution acknowledgment of repository-wide assignment required"
        )
    cfg = config()
    validate_plan(plan, cfg)
    verify_image(plan, directory)
    execution_identity = verify_executor(plan)
    campaign_id = uuid.uuid4().hex
    state = State(directory)
    manager = QueueManager(
        cfg,
        directory,
        CandidateBackend(state, plan, campaign_id),
        clock() + plan["admission_seconds"],
        clock,
    )
    originals = {}
    reason, before_service = "completed", None
    old_handlers = {}
    try:
        with state.lock():
            with budget(300):
                before_service = idle_host(manager)
                originals = protected_images()
                verify_image(plan, directory)
            # The durable drained flag is NEVER cleared by this foreground command.
            manager.admission_end = clock() + plan["admission_seconds"]
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *args: setattr(manager, "stopping", True))
            try:
                with budget(plan["admission_seconds"] + 900):
                    execution_end = clock() + plan["admission_seconds"] + 900

                    def arm() -> None:
                        limit = execution_end
                        for r in state.active():
                            wall_deadline = r["details"].get("candidate_job_deadline")
                            if wall_deadline:
                                limit = min(
                                    limit, clock() + max(0, wall_deadline - time.time())
                                )
                        signal.setitimer(
                            signal.ITIMER_REAL, max(0.001, limit - clock())
                        )

                    manager.arm_deadline = (
                        arm if budget is time_budget else lambda: None
                    )
                    while not manager.stopping:
                        manager.arm_deadline()
                        cast(Callable[[], None], manager.tick)()
                        if manager.state.get("error") or any(
                            r["phase"] == "uncertain" for r in state.active()
                        ):
                            reason = "uncertain_lifecycle"
                            break
                        if clock() >= manager.admission_end and not state.active():
                            break
                        sleep(min(cfg["poll_seconds"], 2))
                    if manager.stopping:
                        reason = "cancelled_or_assignment_failure"
            except CampaignDeadline:
                reason = "execution_deadline"
            except Exception:
                reason = "operational_error"
            finally:
                manager.stopping = True
                try:
                    with budget(plan["cleanup_seconds"]):
                        cleanup_campaign(manager, campaign_id)
                except (CampaignDeadline, Exception):
                    reason = "cleanup_uncertain"
                    for r in state.active():
                        if r["details"].get("candidate_campaign") == campaign_id:
                            state.update(
                                r,
                                "uncertain",
                                error="Campaign cleanup deadline or failure",
                            )
            # Read-only restoration verification. Operator retains control of restart/resume.
            with budget(300):
                execution_unchanged = verify_executor(plan) == execution_identity
                unchanged = protected_images() == originals
                after = execute(
                    [
                        "systemctl",
                        "show",
                        "runspindle-fleet.service",
                        "--property=ActiveState,SubState,UnitFileState",
                    ],
                    check=True,
                )
                fields = dict(
                    line.split("=", 1)
                    for line in after.stdout.splitlines()
                    if "=" in line
                )
                service_unchanged = fields == before_service
            records = receipts(state, campaign_id)
            clean = (
                not state.active()
                and unchanged
                and service_unchanged
                and execution_unchanged
            )
            observed = bool(records) and all(
                r["cleanup_verified"]
                and r["phase"] == "destroyed"
                and r["operational_status"] == "passed"
                and r["actual_claim"].get("conclusion") == "success"
                and r["actual_claim"].get("source_trusted") is True
                for r in records
            )
            report = {
                "status": "passed"
                if clean and observed and reason == "completed"
                else "blocked",
                "reason": reason,
                "evidence_class": "bounded_candidate_queue_observation",
                "campaign_id": campaign_id,
                "execution_controller": execution_identity,
                "image_preparation": {
                    k: plan[k]
                    for k in (
                        "image_source_sha",
                        "controller_sha256",
                        "recipe_sha256",
                        "tool_closure_sha256",
                    )
                },
                "assignment_scope": "repository_queue_not_exact_run",
                "qualified_and_rollback_unchanged": unchanged,
                "service_unchanged": service_unchanged,
                "drained": state.get("drained"),
                "ready_for_operator_restore": clean,
                "receipts": records,
                "rollout_qualification": "not_claimed",
            }
            output = state.directory / ("candidate-queue-" + campaign_id + ".json")
            output.write_text(json.dumps(report, indent=2))
            output.chmod(0o600)
            return report
    finally:
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)
        manager.state.db.close()
        state.db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/runspindle-fleet"
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-repository-queue", action="store_true")
    args = parser.parse_args()
    p = validate_plan(json.loads(args.plan.read_text()), config())
    if args.execute:
        report = run_campaign(p, args.state_dir, args.acknowledge_repository_queue)
        print(json.dumps(report))
        return 0 if report["status"] == "passed" else 2
    verify_image(p, args.state_dir)
    verify_executor(p)
    print(
        json.dumps(
            {
                "status": "plan_valid",
                "execution": "not_run",
                "assignment_scope": "repository_queue_not_exact_run",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
