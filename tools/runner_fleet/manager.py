"""Single-process Strix manager; reservations precede all external operations."""

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import quote

from . import state as _state
from .backend import Backend, execute, helper
from .model import choose_jobs

# Durable SQLite adapter retains its dynamic JSON contract.
State: Any = _state.State


def gh(path: str, method: str = "GET") -> Any:
    return json.loads(
        execute(["gh", "api", "--method", method, path], timeout=45).stdout or "null"
    )


def pages(path: str, key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in range(1, 101):
        separator = "&" if "?" in path else "?"
        value = gh(f"{path}{separator}per_page=100&page={page}")
        rows = value[key]
        result.extend(rows)
        if len(rows) < 100:
            return result
    raise RuntimeError("Pagination limit reached; inventory uncertain")


def trusted(run, repository):
    return (
        run.get("event") in ("push", "workflow_dispatch")
        and (
            run.get("head_branch") == "main"
            or run.get("head_branch", "").startswith("ci/")
        )
        and run.get("head_repository", {}).get("full_name") == repository
    )


class Manager:
    def __init__(self, cfg, directory, backend=None):
        self.cfg = cfg
        self.state = State(directory)
        self.backend: Any = backend or cast(Callable[..., Any], Backend)(self.state)
        self.stopping = False
        self.jobs = []
        self.consumed = set()

    def inventory(self):
        queue, jobs = [], []
        previous = self.state.get("watch_runs", {})
        watch = {}
        active = self.state.active()
        for repo in self.cfg["repositories"]:
            full = self.cfg["owner"] + "/" + repo
            runs = {}
            # Paginate both states: an old queued workflow must never disappear
            # behind a fixed-size list of recent runs.
            for status in ("queued", "in_progress"):
                for run in pages(
                    f"repos/{full}/actions/runs?status={status}", "workflow_runs"
                ):
                    runs[run["id"]] = run
            resources = [r for r in active if r["repository"] == repo]
            ids = set(previous.get(repo, []))
            ids.update(
                j["run_id"]
                for j in self.state.get("queue", [])
                if j["repository"] == repo
            )
            for resource in resources:
                claimed = resource["details"].get("claimed_job", {})
                if claimed.get("run_id"):
                    ids.add(claimed["run_id"])
            if resources:
                earliest = min(r["created"] for r in resources) - 60
                since = datetime.fromtimestamp(earliest, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                for run in pages(
                    f"repos/{full}/actions/runs?created="
                    + quote(">=" + since, safe=""),
                    "workflow_runs",
                ):
                    runs[run["id"]] = run
                # A previously queued old run can finish between polls. Keep
                # watching it until its terminal jobs have been observed once.
                for identity in ids - set(runs):
                    runs[identity] = gh(f"repos/{full}/actions/runs/{identity}")
            watch[repo] = []
            for run in runs.values():
                if not trusted(run, full):
                    continue
                if run["status"] != "completed":
                    watch[repo].append(run["id"])
                for job in pages(f"repos/{full}/actions/runs/{run['id']}/jobs", "jobs"):
                    job = job | {
                        "repository": repo,
                        "run_id": run["id"],
                        "head_sha": run["head_sha"],
                        "event": run["event"],
                        "head_branch": run["head_branch"],
                    }
                    jobs.append(job)
                    if job["status"] == "queued":
                        queue.append(job)
        self.state.set("watch_runs", watch)
        queue.sort(key=lambda job: (job.get("created_at", ""), job["id"]))
        return queue, jobs

    def registrations(self, repo: str) -> list[dict[str, Any]]:
        return pages(f"repos/{self.cfg['owner']}/{repo}/actions/runners", "runners")

    def remove_registration(self, resource: dict[str, Any]) -> None:
        full = f"{self.cfg['owner']}/{resource['repository']}"
        for runner in self.registrations(resource["repository"]):
            if runner["name"] == resource["runner_name"]:
                gh(f"repos/{full}/actions/runners/{runner['id']}", "DELETE")
        if any(
            r["name"] == resource["runner_name"]
            for r in self.registrations(resource["repository"])
        ):
            raise RuntimeError("Runner registration still present")

    def cleanup(self, resource: dict[str, Any], reason: str) -> None:
        conclusion = resource["details"].get("claimed_job", {}).get("conclusion")
        status = (
            "passed"
            if reason == "job success"
            else (
                "failed"
                if conclusion in ("failure", "cancelled", "timed_out")
                or "execution timeout" in reason
                or "forced guest termination" in reason
                else "blocked"
            )
        )
        self.state.update(
            resource, "cleaning", cleanup_reason=reason, operational_status=status
        )
        try:
            self.state.update(
                resource, "cleaning", observed_guest=helper("observe", resource["slot"])
            )
            self.backend.diagnostics(resource)
            self.backend.destroy(resource)
            self.remove_registration(resource)
            self.state.update(resource, "destroyed", cleanup_verified=True)
        except Exception as error:
            self.state.update(
                resource,
                "uncertain",
                cleanup_error=str(error),
                operational_status="blocked",
            )

    def reconcile(self):
        occupied = self.state.active()
        for slot in range(2):
            observed = helper("observe", slot)
            if observed.get("ActiveState") in (
                "active",
                "activating",
                "deactivating",
            ) and not any(r["slot"] == slot for r in occupied):
                # No replacement may launch while an untracked service exists.
                helper("stop", slot, observed["resource_id"])
                directory = Path("/var/lib/runspindle-fleet/slots") / str(slot)
                for name in ("disk.qcow2", "seed.iso", "serial.log"):
                    path = directory / name
                    if path.exists():
                        archive = (
                            self.state.directory
                            / f"orphan-{slot}-{int(time.time())}-{name}"
                        )
                        path.rename(archive)
        for resource in self.state.active():
            observed = helper("observe", resource["slot"])
            if (
                resource["phase"] not in ("running", "finishing")
                or observed.get("ActiveState") != "active"
            ):
                self.cleanup(resource, "restart reconciliation")
        names = {r["runner_name"] for r in self.state.active()}
        for repo in self.cfg["repositories"]:
            for runner in self.registrations(repo):
                if (
                    runner["name"].startswith("runspindle-fleet-")
                    and runner["name"] not in names
                ):
                    gh(
                        f"repos/{self.cfg['owner']}/{repo}/actions/runners/{runner['id']}",
                        "DELETE",
                    )
        self.state.set("reconciled_at", time.time())

    def cancel_claim(self, resource: dict[str, Any], job: dict[str, Any]) -> Any:
        return cast(Callable[..., Any], gh)(
            f"repos/{self.cfg['owner']}/{resource['repository']}/actions/runs/{job['run_id']}/cancel",
            "POST",
        )

    def advance(self, resource, jobs):
        now = time.time()
        try:
            if resource["phase"] in ("reserved", "uncertain", "cleaning"):
                self.cleanup(resource, "unfinished lifecycle")
            elif resource["phase"] == "starting":
                if now > resource["details"]["boot_deadline"]:
                    self.cleanup(resource, "boot timeout")
                elif self.backend.ready(resource):
                    self.backend.isolation(resource)
                    full = f"{self.cfg['owner']}/{resource['repository']}"
                    token = gh(
                        f"repos/{full}/actions/runners/registration-token", "POST"
                    )["token"]
                    self.state.update(
                        resource, "registering", registration_deadline=now + 120
                    )
                    self.backend.register(
                        resource,
                        full,
                        token,
                        self.cfg["lanes"]["strix-linux-x64"]["labels"],
                    )
                    registration = next(
                        (
                            r
                            for r in self.registrations(resource["repository"])
                            if r["name"] == resource["runner_name"]
                        ),
                        None,
                    )
                    if not registration:
                        raise RuntimeError("Registration not observed")
                    self.state.update(
                        resource,
                        "running",
                        runner_id=registration["id"],
                        registered_at=time.time(),
                    )
            elif resource["phase"] == "finishing":
                job = resource["details"]["claimed_job"]
                exited = self.backend.runner_exited(resource)
                if exited or now > resource["details"]["finish_deadline"]:
                    try:
                        result = execute(
                            [
                                "gh",
                                "api",
                                f"repos/{self.cfg['owner']}/{resource['repository']}/actions/jobs/{job['id']}/logs",
                            ],
                            timeout=45,
                        )
                        (
                            self.backend.directory(resource) / "github-job.log"
                        ).write_text(result.stdout)
                    except Exception as error:
                        self.state.update(resource, "finishing", log_error=str(error))
                    self.cleanup(
                        resource,
                        "job "
                        + str(job.get("conclusion"))
                        + ("" if exited else "; runner exit deadline exceeded"),
                    )
            elif resource["phase"] == "running":
                claimed = [
                    j for j in jobs if j.get("runner_name") == resource["runner_name"]
                ]
                if len(claimed) > 1:
                    raise RuntimeError("Ephemeral runner claimed multiple jobs")
                if claimed:
                    job = claimed[0]
                    self.state.update(resource, "running", claimed_job=job)
                    self.consumed.add((resource["repository"], job["id"]))
                    if now - resource["details"].get("diagnostic_at", 0) >= 30:
                        try:
                            self.backend.diagnostics(resource)
                            self.state.update(resource, "running", diagnostic_at=now)
                        except Exception as error:
                            self.state.update(
                                resource, "running", diagnostic_error=str(error)
                            )
                    if job["status"] == "completed":
                        self.state.update(
                            resource,
                            "finishing",
                            claimed_job=job,
                            finish_deadline=now + 120,
                        )
                    elif (
                        helper("observe", resource["slot"]).get("ActiveState")
                        != "active"
                    ):
                        self.cancel_claim(resource, job)
                        self.cleanup(resource, "forced guest termination during job")
                    elif (
                        job.get("started_at")
                        and now
                        - datetime.fromisoformat(
                            job["started_at"].replace("Z", "+00:00")
                        ).timestamp()
                        > self.cfg["job_timeout_seconds"]
                    ):
                        self.cancel_claim(resource, job)
                        self.cleanup(resource, "execution timeout: failed evidence")
                elif now - resource["details"]["registered_at"] > 180:
                    self.cleanup(
                        resource, "registration-to-job timeout: blocked evidence"
                    )
                elif helper("observe", resource["slot"]).get("ActiveState") != "active":
                    self.cleanup(resource, "forced guest termination")
            else:
                self.cleanup(resource, "interrupted registration")
        except Exception as error:
            self.state.update(resource, "uncertain", error=str(error))

    def reserve_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self.state.reserve(job))

    def can_admit(self) -> bool:
        return not self.state.get("drained", True) and not self.state.get("error")

    def tick(self, poll=True):
        if poll:
            queue, self.jobs = self.inventory()
            self.state.set("queue", queue)
            self.state.set("poll_at", time.time())
            self.consumed.clear()
        else:
            queue = self.state.get("queue", [])
        jobs = self.jobs
        for resource in self.state.active():
            self.advance(resource, jobs)
        active = self.state.active()
        reserved_jobs = {(r["repository"], r["requested_job"]) for r in active}
        queue = [
            j
            for j in queue
            if (j["repository"], j["id"]) not in reserved_jobs | self.consumed
        ]
        selected, cursor = cast(Callable[..., Any], choose_jobs)(
            self.cfg,
            "strix",
            queue,
            active,
            self.state.get("cursor", 0),
            not self.can_admit(),
        )
        for job in selected:
            if not self.can_admit():
                break
            resource = self.reserve_job(job)
            self.consumed.add((job["repository"], job["id"]))
            # Cursor is persisted alongside each actual reservation, before boot.
            index = self.cfg["repositories"].index(job["repository"])
            self.state.set("cursor", (index + 1) % len(self.cfg["repositories"]))
            try:
                self.backend.create(resource)
            except Exception as error:
                self.state.update(resource, "uncertain", error=str(error))
        self.state.set("heartbeat", time.time())
        self.state.set(
            "peak_slots", max(self.state.get("peak_slots", 0), len(self.state.active()))
        )

    def run(self):
        os.umask(0o077)
        with self.state.lock():
            signal.signal(signal.SIGTERM, lambda *args: setattr(self, "stopping", True))
            self.reconcile()
            next_poll = 0
            while not self.stopping:
                poll = time.monotonic() >= next_poll
                try:
                    if poll:
                        next_poll = time.monotonic() + self.cfg["poll_seconds"]
                        # Successful inventory is required to clear API errors.
                        queue, self.jobs = self.inventory()
                        self.state.set("queue", queue)
                        self.state.set("poll_at", time.time())
                        self.consumed.clear()
                        self.state.set("error", None)
                    self.tick(poll=False)
                    if poll:
                        repos = {r["repository"] for r in self.state.active()}
                        self.state.set(
                            "runners",
                            {
                                repo: [
                                    r
                                    for r in self.registrations(repo)
                                    if r["name"].startswith("runspindle-fleet-")
                                ]
                                for repo in repos
                            },
                        )
                except Exception as error:
                    self.state.set("error", str(error))
                for _ in range(2):
                    if self.stopping:
                        break
                    time.sleep(1)
            # SIGTERM preserves guests and reservations for restart recovery.
            self.state.set("stopped_at", time.time())
