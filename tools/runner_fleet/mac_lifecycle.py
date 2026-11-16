"""Fail-closed Mac fleet ledger and owned-guest cleanup preparation.

This module is intentionally not connected to ``fleet.py manage``. A Tart or
Lima guest must not be started on their default network before a host-enforced
isolation backend, pinned image closure and live canaries are qualified.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, TypedDict

from .model import admissible_mac_job, choose_jobs, matching_lane, validate_config
from .state import State

_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_BACKENDS = {"macbook-linux-arm64": "lima", "macbook-macos-arm64": "tart"}


# Configurations and persisted/API records follow the existing dynamic JSON contract.
Record = dict[str, Any]


class GitHubReader(Protocol):
    def get(self, path: str) -> Any: ...
    def pages(self, path: str, key: str) -> list[Record]: ...


class GuestCommands(Protocol):
    def names(self, backend: str) -> dict[str, bool]: ...
    def stop(self, backend: str, name: str) -> None: ...
    def delete(self, backend: str, name: str) -> None: ...


class GuestBackend(Protocol):
    def mark_owner(self, resource: Record) -> None: ...
    def create(self, resource: Record) -> None: ...
    def untracked(self, active: list[Record]) -> set[tuple[str, str]]: ...
    def destroy(self, resource: Record) -> None: ...


class JobObservation(TypedDict):
    evidence_class: str
    qualification: str
    queue: list[Record]
    terminal: list[Record]
    queue_expired: list[Record]


class MacBlocked(RuntimeError):
    """A prerequisite or independent observation is unavailable."""


def _require_active_config(cfg: Record) -> None:
    try:
        validate_config(cfg)
    except (KeyError, TypeError, ValueError) as error:
        raise MacBlocked("Mac active fleet repository policy changed") from error
    if cfg["hosts"]["macbook"]["capacity"] != 1:
        raise MacBlocked("Mac capacity policy changed")


def _guest_name(resource: Record) -> str:
    if not _ID.fullmatch(resource["id"]):
        raise MacBlocked("Invalid fleet resource identity")
    return f"runspindle-fleet-{resource['id']}"


def _binding(cfg: Record, job: Record, image: Record) -> Record:
    if not admissible_mac_job(cfg, job):
        raise MacBlocked("Mac job lacks exact repository, workflow or lane admission")
    if job.get("event") != "workflow_dispatch":
        raise MacBlocked("Mac qualification requires a manual dispatch event")
    lane = matching_lane(cfg, "macbook", job["labels"])
    if lane is None or lane not in _BACKENDS:
        raise MacBlocked("Mac job has no supported lane binding")
    if not isinstance(image, dict) or not _SHA.fullmatch(str(image.get("sha256", ""))):
        raise MacBlocked("Exact guest image digest unavailable")
    if image.get("lane") != lane or image.get("backend") != _BACKENDS[lane]:
        raise MacBlocked("Guest image OS, architecture or backend binding mismatch")
    if type(job.get("run_id")) is not int or job["run_id"] <= 0:
        raise MacBlocked("Authoritative workflow run identity unavailable")
    return {
        "host": "macbook",
        "lane": lane,
        "backend": _BACKENDS[lane],
        "image_sha256": image["sha256"],
        "image_qualification": "not_run",
        "head_sha": job["head_sha"],
        "workflow_path": job["workflow_path"],
        "run_id": job["run_id"],
    }


def _github_time(value: object) -> float:
    if not isinstance(value, str):
        raise MacBlocked("GitHub job creation time unavailable")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MacBlocked("GitHub job creation time malformed") from error
    if instant.tzinfo is None:
        raise MacBlocked("GitHub job creation time lacks timezone")
    return instant.astimezone(timezone.utc).timestamp()


class MacGitHub:
    """Read-only authoritative API adapter; no dispatch or runner mutation."""

    def get(self, path: str) -> Any:
        result = subprocess.run(
            ["gh", "api", path], text=True, capture_output=True, timeout=45, check=False
        )
        if result.returncode:
            raise MacBlocked(f"GitHub inventory unavailable ({result.returncode})")
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise MacBlocked("GitHub inventory malformed") from error

    def pages(self, path: str, key: str) -> list[Record]:
        rows: list[Record] = []
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            value = self.get(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(value, dict) or not isinstance(value.get(key), list):
                raise MacBlocked("GitHub paginated inventory malformed")
            rows.extend(value[key])
            if len(value[key]) < 100:
                return rows
        raise MacBlocked("GitHub inventory pagination limit reached")


class MacCommands:
    """Read-only VM inventory and narrowly scoped stop/delete operations.

    A separate owner-operated install must later bind this adapter to a
    dedicated account and prove the real network and credential context.
    """

    def run(self, argv: list[str]) -> str:
        result = subprocess.run(
            argv, text=True, capture_output=True, timeout=30, check=False
        )
        if result.returncode:
            raise MacBlocked(
                f"{Path(argv[0]).name} operation failed ({result.returncode})"
            )
        return result.stdout

    def names(self, backend: str) -> dict[str, bool]:
        if backend == "tart":
            rows = json.loads(self.run(["tart", "list", "--format", "json"]))
            if not isinstance(rows, list):
                raise MacBlocked("Tart inventory malformed")
            return {
                row["Name"]: bool(row["Running"])
                for row in rows
                if row.get("Source") == "local"
            }
        if backend == "lima":
            output = self.run(["limactl", "list", "--format", "json"])
            # Lima emits empty stdout when no instance exists on this host.
            rows = json.loads(output) if output.strip() else []
            if not isinstance(rows, list):
                raise MacBlocked("Lima inventory malformed")
            return {row["Name"]: row["Status"] == "Running" for row in rows}
        raise MacBlocked("Unknown Mac backend")

    def stop(self, backend: str, name: str) -> None:
        if backend == "tart":
            self.run(["tart", "stop", name, "--timeout", "20"])
        elif backend == "lima":
            self.run(["limactl", "stop", name])
        else:
            raise MacBlocked("Unknown Mac backend")

    def delete(self, backend: str, name: str) -> None:
        if backend == "tart":
            self.run(["tart", "delete", name])
        elif backend == "lima":
            self.run(["limactl", "delete", name])
        else:
            raise MacBlocked("Unknown Mac backend")


class MacBackend:
    def __init__(self, state: State, commands: GuestCommands | None = None) -> None:
        self.state = state
        self.commands = commands or MacCommands()

    def mark_owner(self, resource: Record) -> None:
        details = resource["details"]
        if resource.get("runner_name") != _guest_name(resource):
            raise MacBlocked("Runner and guest identity differ")
        directory = self.state.directory / "resources" / resource["id"]
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        marker = directory / "owner.json"
        if marker.exists():
            raise MacBlocked("Guest ownership marker already exists")
        owner = {
            "resource_id": resource["id"],
            "guest_name": _guest_name(resource),
            "backend": details["backend"],
            "image_sha256": details["image_sha256"],
            "lease_epoch": details["lease_epoch"],
        }
        descriptor, temporary = tempfile.mkstemp(prefix=".owner-", dir=directory)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(owner, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            marker.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _owned(self, resource: Record) -> tuple[str, str]:
        details = resource["details"]
        if resource.get("runner_name") != _guest_name(resource):
            raise MacBlocked("Runner and guest identity differ")
        if (
            details.get("host") != "macbook"
            or details.get("backend") not in _BACKENDS.values()
        ):
            raise MacBlocked("Resource has no Mac ownership binding")
        if details.get("guest_name") != _guest_name(resource):
            raise MacBlocked("Guest ownership marker mismatch")
        marker = self.state.directory / "resources" / resource["id"] / "owner.json"
        try:
            owner = json.loads(marker.read_text())
        except (OSError, ValueError):
            raise MacBlocked("Durable guest owner marker unavailable")
        if owner != {
            "resource_id": resource["id"],
            "guest_name": details["guest_name"],
            "backend": details["backend"],
            "image_sha256": details["image_sha256"],
            "lease_epoch": details["lease_epoch"],
        }:
            raise MacBlocked("Durable guest owner marker changed")
        return details["backend"], details["guest_name"]

    def create(self, resource: Record) -> None:
        # No Tart/Lima default NAT or host-only network is an authorized
        # substitute for the required host-side allowlist and live canaries.
        raise MacBlocked(
            "Mac guest boot blocked: image, egress and canaries unqualified"
        )

    def untracked(self, active: list[Record]) -> set[tuple[str, str]]:
        owned = {
            (r["details"].get("backend"), r["details"].get("guest_name"))
            for r in active
        }
        found: set[tuple[str, str]] = set()
        for backend in ("lima", "tart"):
            found.update(
                (backend, name)
                for name in self.commands.names(backend)
                if name.startswith("runspindle-fleet-") and (backend, name) not in owned
            )
        return found

    def destroy(self, resource: Record) -> None:
        backend, name = self._owned(resource)
        observed = self.commands.names(backend)
        if name in observed and observed[name]:
            self.commands.stop(backend, name)
            observed = self.commands.names(backend)
            if observed.get(name):
                raise MacBlocked("Guest termination uncertain")
        if name in observed:
            self.commands.delete(backend, name)
        if name in self.commands.names(backend):
            raise MacBlocked("Guest still present after deletion")


class MacManager:
    """Single-slot admission and recovery, with no activation entrypoint."""

    def __init__(
        self,
        cfg: Record,
        directory: str | Path,
        backend: GuestBackend | None = None,
        reader: GitHubReader | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        _require_active_config(cfg)
        self.cfg = deepcopy(cfg)
        self.state = State(directory)
        self.backend = backend or MacBackend(self.state)
        self.reader = reader or MacGitHub()
        self.clock = clock or time.time

    def observe_jobs(
        self, reader: GitHubReader | None = None, now: float | None = None
    ) -> JobObservation:
        """Fetch GitHub run/job identity without dispatching or trusting a receipt.

        The returned queue is only a candidate for the still-blocked backend.
        Completed jobs are observations, never workload or isolation passes.
        API failure leaves the prior watch and terminal ledger intact.
        """
        _require_active_config(self.cfg)
        reader = reader or self.reader
        now = self.clock() if now is None else now
        with self.state.lock():
            prior = self.state.get("mac_watch_runs", {})
            if not isinstance(prior, dict):
                raise MacBlocked("Mac watch ledger malformed")
            active = self.state.active()
            queue: list[Record] = []
            terminal: list[Record] = []
            expire: list[Record] = []
            watches: dict[str, list[int]] = {}
            seen_jobs: dict[tuple[str, int], Record] = {}
            for repo in self.cfg["repositories"]:
                full = f"{self.cfg['owner']}/{repo}"
                runs: dict[int, Record] = {}
                for status in ("queued", "in_progress"):
                    for run in reader.pages(
                        f"repos/{full}/actions/runs?status={status}", "workflow_runs"
                    ):
                        identity = run.get("id")
                        if type(identity) is not int or identity <= 0:
                            raise MacBlocked("GitHub workflow run identity malformed")
                        if identity in runs and runs[identity] != run:
                            raise MacBlocked("Ambiguous GitHub workflow run")
                        runs[identity] = run
                remembered = prior.get(repo, [])
                if not isinstance(remembered, list) or any(
                    type(identity) is not int or identity <= 0
                    for identity in remembered
                ):
                    raise MacBlocked("Mac watched run identities malformed")
                watched = set(remembered)
                for resource in active:
                    if resource["repository"] == repo:
                        run_id = resource["details"].get("run_id")
                        if type(run_id) is not int or run_id <= 0:
                            raise MacBlocked("Active Mac resource has no run identity")
                        watched.add(run_id)
                for identity in watched - runs.keys():
                    fetched = reader.get(f"repos/{full}/actions/runs/{identity}")
                    if not isinstance(fetched, dict) or fetched.get("id") != identity:
                        raise MacBlocked("Watched GitHub run identity changed")
                    runs[identity] = fetched
                watches[repo] = []
                for run in runs.values():
                    if not isinstance(run, dict):
                        raise MacBlocked("GitHub workflow run malformed")
                    run_id = run["id"]
                    if run.get("status") not in ("queued", "in_progress", "completed"):
                        raise MacBlocked("GitHub workflow status unknown")
                    if run.get("head_repository", {}).get("full_name") != full:
                        continue
                    if run.get("event") != "workflow_dispatch":
                        continue
                    branch = run.get("head_branch")
                    if not isinstance(branch, str) or not (
                        branch == "main" or branch.startswith("ci/")
                    ):
                        continue
                    if run.get("path") != ".github/workflows/fleet-qualification.yml":
                        continue
                    if run["status"] != "completed":
                        watches[repo].append(run_id)
                    jobs = reader.pages(
                        f"repos/{full}/actions/runs/{run_id}/jobs", "jobs"
                    )
                    run_jobs: set[int] = set()
                    for job in jobs:
                        if (
                            not isinstance(job, dict)
                            or type(job.get("id")) is not int
                            or job["id"] <= 0
                        ):
                            raise MacBlocked("GitHub job identity malformed")
                        if job.get("run_id") is not None and job["run_id"] != run_id:
                            raise MacBlocked(
                                "GitHub job run identity conflicts with workflow"
                            )
                        if job.get("head_sha") is not None and job[
                            "head_sha"
                        ] != run.get("head_sha"):
                            raise MacBlocked(
                                "GitHub job revision conflicts with workflow"
                            )
                        if job["id"] in run_jobs:
                            raise MacBlocked("Ambiguous duplicate GitHub job")
                        run_jobs.add(job["id"])
                        candidate = {
                            "id": job["id"],
                            "run_id": run_id,
                            "repository": repo,
                            "name": job.get("name"),
                            "labels": job.get("labels"),
                            "status": job.get("status"),
                            "created_at": job.get("created_at"),
                            "head_repository": full,
                            "head_branch": branch,
                            "head_sha": run.get("head_sha"),
                            "event": run["event"],
                            "workflow_path": run["path"],
                            "conclusion": job.get("conclusion"),
                            "runner_id": job.get("runner_id"),
                            "runner_name": job.get("runner_name"),
                        }
                        if not admissible_mac_job(self.cfg, candidate):
                            continue
                        key = (repo, job["id"])
                        if key in seen_jobs and seen_jobs[key] != candidate:
                            raise MacBlocked(
                                "GitHub job appeared with conflicting identity"
                            )
                        seen_jobs[key] = candidate
                        previous = self.state.mac_job_observation(repo, job["id"])
                        if previous and (previous["run_id"], previous["head_sha"]) != (
                            run_id,
                            run["head_sha"],
                        ):
                            raise MacBlocked(
                                "GitHub job identity changed since durable observation"
                            )
                        if job.get("status") == "completed":
                            terminal.append(candidate)
                        elif job.get("status") == "queued":
                            if run["status"] == "completed":
                                raise MacBlocked(
                                    "Completed run still has queued Mac job"
                                )
                            created = _github_time(job.get("created_at"))
                            if now - created >= self.cfg["queue_timeout_seconds"]:
                                expire.append(candidate)
                            elif previous is None:
                                queue.append(candidate)
                        elif job.get("status") != "in_progress":
                            raise MacBlocked("GitHub job status unknown")
                watches[repo] = sorted(set(watches[repo]))
            # All API calls and identity checks above complete before durable
            # observations change. No caller-provided conclusion is accepted.
            for resource in active:
                matches = [
                    job
                    for job in terminal
                    if job["repository"] == resource["repository"]
                    and job["id"] == resource["requested_job"]
                ]
                if len(matches) > 1:
                    raise MacBlocked("Ambiguous terminal Mac job")
                if matches and (matches[0]["run_id"], matches[0]["head_sha"]) != (
                    resource["details"]["run_id"],
                    resource["details"]["head_sha"],
                ):
                    raise MacBlocked(
                        "Terminal job does not match reserved run/revision"
                    )
            for job in expire:
                previous = self.state.mac_job_observation(job["repository"], job["id"])
                if previous and previous["outcome"] == "completed":
                    raise MacBlocked("Terminal Mac job cannot return to queue")
            for job in expire:
                self.state.record_mac_job(
                    job["repository"],
                    job["id"],
                    job["run_id"],
                    job["head_sha"],
                    "queue_expired",
                )
            for job in terminal:
                self.state.record_mac_job(
                    job["repository"],
                    job["id"],
                    job["run_id"],
                    job["head_sha"],
                    "completed",
                )
            self.state.set("mac_watch_runs", watches)
            for resource in active:
                matches = [
                    job
                    for job in terminal
                    if job["repository"] == resource["repository"]
                    and job["id"] == resource["requested_job"]
                ]
                if matches:
                    job = matches[0]
                    self.state.update(
                        resource,
                        resource["phase"],
                        github_observation={
                            "status": "completed",
                            "conclusion": job["conclusion"],
                            "run_id": job["run_id"],
                            "job_id": job["id"],
                            "runner_id": job["runner_id"],
                            "runner_name": job["runner_name"],
                        },
                    )
            queue.sort(
                key=lambda job: (job["created_at"], job["repository"], job["id"])
            )
            return {
                "evidence_class": "read_only_github_inventory",
                "qualification": "blocked",
                "queue": queue,
                "terminal": terminal,
                "queue_expired": expire,
            }

    def _authoritative_queued_job(self, proposed: Record) -> Record:
        repo, run_id, job_id = (
            proposed.get(key) for key in ("repository", "run_id", "id")
        )
        if (
            not isinstance(repo, str)
            or repo not in self.cfg["repositories"]
            or type(run_id) is not int
            or run_id <= 0
            or type(job_id) is not int
            or job_id <= 0
        ):
            raise MacBlocked("Invalid Mac job identity")
        full = f"{self.cfg['owner']}/{repo}"
        run = self.reader.get(f"repos/{full}/actions/runs/{run_id}")
        if (
            not isinstance(run, dict)
            or run.get("id") != run_id
            or run.get("status") not in ("queued", "in_progress")
        ):
            raise MacBlocked("Authoritative queued run unavailable")
        jobs = self.reader.pages(f"repos/{full}/actions/runs/{run_id}/jobs", "jobs")
        matches = [
            entry
            for entry in jobs
            if isinstance(entry, dict) and entry.get("id") == job_id
        ]
        if len(matches) != 1 or matches[0].get("status") != "queued":
            raise MacBlocked("Authoritative queued job missing or ambiguous")
        entry = matches[0]
        if entry.get("run_id") is not None and entry["run_id"] != run_id:
            raise MacBlocked("GitHub job run identity conflicts with workflow")
        if entry.get("head_sha") is not None and entry["head_sha"] != run.get(
            "head_sha"
        ):
            raise MacBlocked("GitHub job revision conflicts with workflow")
        candidate = {
            "id": job_id,
            "run_id": run_id,
            "repository": repo,
            "name": entry.get("name"),
            "labels": entry.get("labels"),
            "head_repository": run.get("head_repository", {}).get("full_name"),
            "head_branch": run.get("head_branch"),
            "head_sha": run.get("head_sha"),
            "event": run.get("event"),
            "workflow_path": run.get("path"),
        }
        for key, value in candidate.items():
            if proposed.get(key) != value:
                raise MacBlocked("Caller job identity differs from GitHub observation")
        if (
            not admissible_mac_job(self.cfg, candidate)
            or candidate["event"] != "workflow_dispatch"
        ):
            raise MacBlocked("Authoritative Mac job admission denied")
        if (
            self.clock() - _github_time(entry.get("created_at"))
            >= self.cfg["queue_timeout_seconds"]
        ):
            raise MacBlocked("Mac job queue deadline exceeded")
        if self.state.mac_job_observation(repo, job_id):
            raise MacBlocked("Mac job already terminal or expired in durable ledger")
        return candidate

    def reserve_candidate(self, job: Record, image: Record) -> Record:
        _require_active_config(self.cfg)
        with self.state.lock():
            observed = self._authoritative_queued_job(job)
            binding = _binding(self.cfg, observed, image)
            if self.state.active():
                raise MacBlocked("Mac slot occupied by active or uncertain resource")
            if self.backend.untracked([]):
                raise MacBlocked("Untracked fleet-named Mac guest; allocation blocked")
            selected, _ = choose_jobs(
                self.cfg, "macbook", [observed], [], drained=False
            )
            if selected != [observed]:
                raise MacBlocked("Job not selected by Mac fleet policy")
            # The audit event needs the scoped job identity, not GitHub's full
            # payload (which may contain unreviewed or private metadata).
            resource = self.state.reserve(
                {
                    "repository": observed["repository"],
                    "id": observed["id"],
                    "run_id": observed["run_id"],
                    "head_sha": observed["head_sha"],
                    "event": observed["event"],
                    "head_branch": observed["head_branch"],
                },
                capacity=1,
                binding=binding,
            )
            self.state.update(resource, "reserved", guest_name=_guest_name(resource))
            resource = self.state.active()[0]
            self.backend.mark_owner(resource)
            return resource

    def start(self, resource: Record) -> None:
        # The backend is deliberately fail-closed. A lost boot reply must keep
        # the reservation until an independent observation resolves it.
        with self.state.lock():
            resource = self._current(resource)
            try:
                self.backend.create(resource)
            except Exception as error:
                self.state.update(resource, "uncertain", error=str(error))
                raise

    def cleanup(self, resource: Record) -> None:
        with self.state.lock():
            self._cleanup(resource)

    def _cleanup(self, resource: Record) -> None:
        resource = self._current(resource)
        self.state.update(resource, "cleaning")
        try:
            self.backend.destroy(resource)
            self.state.update(resource, "destroyed", cleanup_verified=True)
        except Exception as error:
            self.state.update(resource, "uncertain", cleanup_error=str(error))
            raise

    def _current(self, resource: Record) -> Record:
        rows = [row for row in self.state.active() if row["id"] == resource["id"]]
        if len(rows) != 1:
            raise MacBlocked("Stale Mac resource or lease; refusing mutation")
        current = rows[0]
        fields = ("slot", "repository", "requested_job", "runner_name")
        binding = (
            "host",
            "lane",
            "backend",
            "image_sha256",
            "lease_epoch",
            "head_sha",
            "workflow_path",
            "run_id",
            "guest_name",
        )
        if any(current[key] != resource.get(key) for key in fields) or any(
            current["details"].get(key) != resource.get("details", {}).get(key)
            for key in binding
        ):
            raise MacBlocked("Stale Mac resource or lease; refusing mutation")
        return current

    def reconcile(self) -> None:
        # Unknown fleet-named guests cannot be assigned to a new job. This
        # manager never deletes an untracked or personally owned instance.
        with self.state.lock():
            active = self.state.active()
            if self.backend.untracked(active):
                raise MacBlocked("Untracked fleet-named Mac guest; allocation blocked")
            for resource in active:
                self._cleanup(resource)
