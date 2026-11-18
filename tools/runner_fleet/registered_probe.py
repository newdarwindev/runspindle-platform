"""One owner-approved repository runner, never a broad manager or exact-job gate.

Source-only until independent review and deployed recovery-guard verification.
GitHub can assign ANY eligible job in the selected repository. No hook is an
admission boundary. The existing guest isolation and host watchdog remain required.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import re
import argparse
import signal
import shutil
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

from .backend import ROOT, Backend, execute, helper
from .candidate import time_budget
from .manager import gh, pages
from .single_probe import (
    GUEST_SCRIPT,
    IsolatedMetadata,
    manager_stopped,
    read_regular,
    validate_observation,
    validate_state_directory,
    verify_guest_lifetime,
    verify_host_lifetime_policy,
    plan,
    terminated,
    STATE_DIRECTORY,
)
from .offline_probe import write
from .state import State

REPOS = ("runspindle-browser", "runspindle-core", "runspindle-host")
IMAGE_SHA = "a5db32338fd6721c53e5b5078d82220c4186181a25da28b6aee726787f632cd0"
CAMPAIGN = "approved-three-repo-20261118-v1"
GIB = 1024**3
EXPECTED_MANAGER = "5fbb8157580a34c841ba016ec15e013f441603130c12ab48ce8377a160a78b0c"


def guarded_installation() -> None:
    for path in (
        Path(__file__).with_name("manager.py"),
        Path("/opt/runspindle-fleet/tools/runner_fleet/manager.py"),
    ):
        if hashlib.sha256(read_regular(path)).hexdigest() != EXPECTED_MANAGER:
            raise ValueError("Installed/local manager differs from frozen guard")
    unit = execute(
        [
            "systemctl",
            "show",
            "runspindle-fleet.service",
            "--property=ExecStart",
            "--value",
        ]
    ).stdout
    if unit.count("argv[]=") != 1:
        raise ValueError("Unexpected manager entrypoint")
    argv = shlex.split(unit.split("argv[]=", 1)[1].split(";", 1)[0].strip())
    if argv != [
        "/usr/bin/python3",
        "/opt/runspindle-fleet/tools/fleet.py",
        "manage",
        "--host",
        "strix",
        "--state-dir",
        "/home/standard/.local/state/runspindle-fleet",
    ]:
        raise ValueError("Unexpected manager entrypoint")


LABELS = ["self-hosted", "linux", "x64", "runspindle-isolated", "strix"]


FLEET_LV = Path("/dev/pve/runspindle-fleet-capacity-20261115")


def storage_identity(directory: Path) -> dict[str, int]:
    if directory != STATE_DIRECTORY:
        raise ValueError("Unexpected snapshot destination")
    device = FLEET_LV.stat()
    if not stat.S_ISBLK(device.st_mode):
        raise ValueError("Expected fleet block device missing")
    expected = f"{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}"
    result = {}
    for name, path in [
        ("state", directory),
        ("fleet", ROOT),
        ("slots", ROOT / "slots"),
        ("retained", ROOT / "retained"),
    ]:
        if path.resolve(strict=True) != path or path.stat().st_dev != device.st_rdev:
            raise ValueError("Unexpected fleet storage device")
        mounts = json.loads(
            execute(
                ["findmnt", "-J", "-T", str(path), "-o", "SOURCE,FSTYPE,MAJ:MIN"],
                timeout=20,
            ).stdout
        )["filesystems"]
        if (
            len(mounts) != 1
            or mounts[0]["fstype"] != "ext4"
            or mounts[0]["maj:min"] != expected
        ):
            raise ValueError("Unexpected fleet mount identity")
        source = mounts[0]["source"].split("[", 1)[0]
        if Path(source).resolve(strict=True) != FLEET_LV.resolve(strict=True):
            raise ValueError("Mount source is not approved fleet LV")
        result[name] = shutil.disk_usage(path).free
    rows = json.loads(
        execute(
            [
                "sudo",
                "-n",
                "lvs",
                "--reportformat",
                "json",
                "-o",
                "vg_name,lv_name,pool_lv",
                str(FLEET_LV),
            ],
            timeout=20,
        ).stdout
    )["report"][0]["lv"]
    if rows != [
        {
            "vg_name": "pve",
            "lv_name": "runspindle-fleet-capacity-20261115",
            "pool_lv": "data",
        }
    ]:
        raise ValueError("Unexpected fleet backing pool")
    return result


def capacity(directory: Path) -> dict[str, Any]:
    """Conservative allowances, not hard quotas; pool and filesystem both checked."""
    available = (
        int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        )
        * 1024
    )
    disks = storage_identity(directory)
    disk = min(disks.values())
    raw = execute(
        [
            "sudo",
            "-n",
            "lvs",
            "--reportformat",
            "json",
            "--units",
            "b",
            "--nosuffix",
            "-o",
            "lv_size,data_percent,metadata_percent",
            "pve/data",
        ],
        timeout=20,
    ).stdout
    rows = json.loads(raw)["report"][0]["lv"]
    if len(rows) != 1:
        raise ValueError("Ambiguous backing pool")
    row = rows[0]
    physical = float(row["lv_size"]) * (1 - float(row["data_percent"]) / 100)
    metadata = float(row["metadata_percent"])
    # 49GiB covers original+snapshot+qcow metadata, then 30GiB reserve.
    if (
        not all(math.isfinite(x) for x in (physical, metadata))
        or available < 13 * GIB
        or disk < 79 * GIB
        or physical < 79 * GIB
        or not 0 <= metadata < 80
    ):
        raise ValueError(
            "Insufficient independently checked RAM/filesystem/pool reserve"
        )
    return {
        "available_memory": available,
        "filesystem_free": disk,
        "filesystem_paths_free": disks,
        "pool_free": int(physical),
        "pool_metadata_percent": metadata,
    }


def registrations(repo: str) -> list[dict[str, Any]]:
    return pages(f"repos/newdarwindev/{repo}/actions/runners", "runners")


def remove_owned(repo: str, resource: dict[str, Any]) -> None:
    for row in registrations(repo):
        if row["name"] == resource["runner_name"]:
            gh(f"repos/newdarwindev/{repo}/actions/runners/{int(row['id'])}", "DELETE")
    if any(row["name"] == resource["runner_name"] for row in registrations(repo)):
        raise RuntimeError("Owned registration remains")


def observe_jobs(
    repo: str, runner: str, since: str, prior_runs: list[int]
) -> list[dict[str, Any]]:
    # Actual assignment is observed after the fact, never promised in advance.
    runs = pages(
        f"repos/newdarwindev/{repo}/actions/runs?created=%3E%3D{since}", "workflow_runs"
    )
    known = {int(run["id"]): run for run in runs}
    for run_id in prior_runs:
        if run_id not in known:
            run = gh(f"repos/newdarwindev/{repo}/actions/runs/{run_id}")
            if int(run["id"]) != run_id:
                raise ValueError("Historical run identity mismatch")
            known[run_id] = run
    result = []
    for run in known.values():
        for job in pages(
            f"repos/newdarwindev/{repo}/actions/runs/{run['id']}/jobs", "jobs"
        ):
            if job.get("runner_name") == runner:
                result.append(
                    {
                        k: job.get(k)
                        for k in (
                            "id",
                            "name",
                            "status",
                            "conclusion",
                            "head_sha",
                            "started_at",
                            "completed_at",
                        )
                    }
                    | {"run_id": run["id"], "run_attempt": run["run_attempt"]}
                )
    return result


def cleanup(
    state: Any, backend: Any, resource: dict[str, Any], repo: str
) -> dict[str, Any]:
    errors: list[str] = []
    # Each effect independently attempted even if persistence/removal fails.
    try:
        with time_budget(15):
            state.update(resource, "cleaning")
    except BaseException as error:
        errors.append("state:" + type(error).__name__)
    try:
        with time_budget(30):
            helper("stop", resource["slot"], resource["id"])
    except BaseException as error:
        errors.append("stop:" + type(error).__name__)
    try:
        with time_budget(120):
            remove_owned(repo, resource)
    except BaseException as error:
        errors.append("registration:" + type(error).__name__)
    try:
        with time_budget(650):
            backend.destroy(resource)  # Approved one retained synthetic snapshot.
            observed = helper("observe", resource["slot"])
            if observed.get("ActiveState") not in ("inactive", "failed"):
                raise ValueError("Guest stop uncertain")
    except BaseException as error:
        errors.append("retention:" + type(error).__name__)
    try:
        with time_budget(15):
            state.update(
                resource,
                "uncertain" if errors else "destroyed",
                cleanup_verified=not errors,
                cleanup_error_types=errors,
            )
    except BaseException as error:
        errors.append("final-state:" + type(error).__name__)
    return {
        "cleanup": "blocked" if errors else "verified",
        "cleanup_error_types": errors,
    }


def run(intended: dict[str, Any], state: State, repo: str) -> dict[str, Any]:
    """Caller must first verify frozen installed guard, source and candidate pins.

    No public CLI currently exposes execution; reviewed driver integration pending.
    """
    if repo not in REPOS:
        raise ValueError("Outside approved repositories")
    if intended.get("bindings", {}).get("image_sha256") != IMAGE_SHA:
        raise ValueError("Outside approved exact image")
    guarded_installation()
    validate_state_directory(state.directory)
    manager_stopped()
    verify_host_lifetime_policy()
    with state.lock():
        manager_stopped()
        if state.active():
            raise ValueError("Occupied or uncertain shared capacity")
        for slot in range(2):
            observed = helper("observe", slot)
            if observed.get("ActiveState") not in ("inactive", "failed"):
                raise ValueError("Unknown or occupied host guest")
        history = state.get(CAMPAIGN, [])
        if (
            not isinstance(history, list)
            or any(x not in REPOS for x in history)
            or len(history) != len(set(history))
            or repo in history
            or len(history) >= 3
        ):
            raise ValueError("Campaign attempt unavailable")
        budget = capacity(state.directory)
        if any(registrations(r) for r in REPOS):
            raise ValueError("Existing registration; no competing admission")
        prior_runs = sorted(
            {
                int(run["id"])
                for status in ("queued", "in_progress")
                for run in pages(
                    f"repos/newdarwindev/{repo}/actions/runs?status={status}",
                    "workflow_runs",
                )
            }
        )
        # Record attempt BEFORE reservation so interruption cannot repeat it.
        state.set(CAMPAIGN, history + [repo])
        directory = state.directory / ("registered-probe-" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        write(
            directory / "candidate.json",
            (json.dumps(intended["manifest"]) + "\n").encode(),
        )
        resource = state.reserve(
            {"repository": repo, "id": -1},
            details={
                "manual_registered_probe": True,
                "prior_run_ids": prior_runs,
                "probe_directory": directory.name,
                "probe_image": intended["image"],
                "image_sha256": intended["bindings"]["image_sha256"],
            },
        )
        backend = Backend(IsolatedMetadata(state, directory), intended["image"])
        report: dict[str, Any] = {
            "status": "failed",
            "resource": resource["id"],
            "repository": repo,
            "budget": budget,
            "bindings": intended["bindings"],
            "ci_qualified": False,
            "promoted": False,
        }
        try:
            with time_budget(1250):
                backend.create(resource)
                verify_guest_lifetime(resource["slot"])
                deadline = time.monotonic() + 240
                while not backend.ready(resource):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Guest startup")
                    time.sleep(2)
                observed = json.loads(
                    backend.ssh(resource, GUEST_SCRIPT, timeout=60).stdout
                )
                validate_observation(observed, intended, resource)
                backend.isolation(resource)
                # Short-lived registration credential only through existing stdin path.
                token = gh(
                    f"repos/newdarwindev/{repo}/actions/runners/registration-token",
                    "POST",
                )["token"]
                try:
                    backend.register(resource, "newdarwindev/" + repo, token, LABELS)
                finally:
                    token = None
                report["registered"] = True
                deadline = time.monotonic() + 900
                while not backend.runner_exited(resource):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("One runner job/admission budget")
                    time.sleep(5)
                # Receipt observation separate from execution success and qualification.
                from datetime import datetime, timezone

                since = datetime.fromtimestamp(
                    resource["created"] - 60, timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                jobs = observe_jobs(repo, resource["runner_name"], since, prior_runs)
                report["jobs"] = jobs
                if len(jobs) != 1 or jobs[0]["status"] != "completed":
                    raise ValueError("Single terminal assignment not established")
                report["status"] = (
                    "passed" if jobs[0]["conclusion"] == "success" else "failed"
                )
        except BaseException as error:
            report["error_type"] = type(error).__name__
        finally:
            report.update(cleanup(state, backend, resource, repo))
            if report["cleanup"] != "verified":
                report["status"] = "failed"
            write(
                directory / "result.json",
                (json.dumps(report, indent=2) + "\n").encode(),
            )
        return report


def recover(state: State, identity: str) -> dict[str, Any]:
    """Stop/remove only. Never create another snapshot or release uncertain files.

    This is explicit crash recovery for the durable registered resource, not a
    new campaign attempt. Original disks/seed/key/logs remain wherever stopped.
    A subsequent reviewed storage reconciliation is required to release the slot.
    """
    guarded_installation()
    validate_state_directory(state.directory)
    manager_stopped()
    if not re.fullmatch("[0-9a-f]{32}", identity):
        raise ValueError("Exact resource identity required")
    with state.lock():
        manager_stopped()
        rows = [r for r in state.active() if r["id"] == identity]
        if len(rows) != 1:
            raise ValueError("Exact occupied resource required")
        resource = rows[0]
        details = resource["details"]
        if (
            details.get("manual_registered_probe") is not True
            or details.get("image_sha256") != IMAGE_SHA
            or resource["repository"] not in REPOS
            or resource["runner_name"] != "runspindle-fleet-" + identity
        ):
            raise ValueError("Recovery binding differs")
        errors = []
        try:
            with time_budget(30):
                helper("stop", resource["slot"], identity)
                if helper("observe", resource["slot"]).get("ActiveState") not in (
                    "inactive",
                    "failed",
                ):
                    raise ValueError("Stop not verified")
        except BaseException as error:
            errors.append("stop:" + type(error).__name__)
        try:
            with time_budget(120):
                remove_owned(resource["repository"], resource)
        except BaseException as error:
            errors.append("registration:" + type(error).__name__)
        # Intentionally occupied even after verified stop: no silent data cleanup.
        state.update(
            resource,
            "uncertain",
            recovery_stopped=not errors,
            recovery_error_types=errors,
            recovery_note="original files preserved; explicit storage reconciliation required",
        )
        return {
            "resource": identity,
            "recovery_stopped": not errors,
            "errors": errors,
            "capacity_released": False,
            "snapshot_created": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", choices=REPOS)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--recover", help="Exact occupied resource; stop/remove only, preserve files"
    )
    args = parser.parse_args()
    if args.recover:
        if any(
            (args.repository, args.image, args.manifest, args.expected, args.execute)
        ):
            parser.error("Recovery cannot be combined with execution arguments")
        guarded_installation()
        validate_state_directory(STATE_DIRECTORY)
        previous = signal.signal(signal.SIGTERM, terminated)
        state = None
        try:
            state = State(STATE_DIRECTORY)
            result = recover(state, args.recover)
            print(json.dumps(result))
            return 0 if result["recovery_stopped"] else 1
        finally:
            if state is not None:
                state.db.close()
            signal.signal(signal.SIGTERM, previous)
    if not all((args.repository, args.image, args.manifest, args.expected)):
        parser.error("Execution/plan requires repository,image,manifest,expected")
    intended = plan(args.image, args.manifest, args.expected)
    if intended["bindings"]["image_sha256"] != IMAGE_SHA:
        raise ValueError("Outside approved exact image")
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "planned",
                    "repository": args.repository,
                    "bindings": intended["bindings"],
                    "registered": False,
                }
            )
        )
        return 0
    guarded_installation()
    verify_host_lifetime_policy()
    validate_state_directory(STATE_DIRECTORY)
    manager_stopped()
    previous = signal.signal(signal.SIGTERM, terminated)
    state = None
    try:
        state = State(STATE_DIRECTORY)
        result = run(intended, state, args.repository)
        print(json.dumps(result))
        return 0 if result["status"] == "passed" else 1
    finally:
        if state is not None:
            state.db.close()
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
