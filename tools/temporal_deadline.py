#!/usr/bin/env python3
"""Explicit approved-test supervisor; not a service or automatic admission path.

Source-only until reviewed and separately approved for Docker bridge effects.
900s test budget +30s graceful stop +10s kill wait +180s owned cleanup. Prefix
and synthetic label must both match. Unknown identities retain evidence and fail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LABEL = "runspindle.synthetic=true"


def owns(name: str, run_id: str, kind: str) -> bool:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("invalid controller nonce")
    suffix = {
        "container": r"(?:server(?:-[a-f0-9]{12})?|client-[a-f0-9]{12})",
        "volume": "db",
        "network": "net",
    }[kind]
    return (
        re.fullmatch("runspindle-temporal-" + run_id + r"-[a-f0-9]{32}-" + suffix, name)
        is not None
    )


def docker(args: list[str], deadline: float) -> str:
    remaining = min(15.0, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("cleanup deadline")
    value = subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=remaining
    )
    if len(value.stdout) > 128 * 1024:
        raise ValueError("Docker observation bound")
    return value.stdout


def inventory(kind: str, run_id: str, deadline: float) -> list[tuple[str, str]]:
    prefix = "runspindle-temporal-" + run_id + "-"
    args = [kind, "ls"] + (["-a"] if kind == "container" else [])
    fmt = (
        "{{.ID}} {{.Names}}"
        if kind == "container"
        else ("{{.ID}} {{.Name}}" if kind == "network" else "{{.Name}} {{.Name}}")
    )
    rows = docker(
        [
            *args,
            "--filter",
            "label=" + LABEL,
            "--filter",
            "name=" + prefix,
            "--format",
            fmt,
        ],
        deadline,
    ).splitlines()
    if len(rows) > 32:
        raise ValueError("owned resource bound")
    result = []
    for row in rows:
        identity, name = row.split()
        if not owns(name, run_id, kind):
            raise ValueError("unexpected resource name")
        data = json.loads(docker([kind, "inspect", identity], deadline))
        if len(data) != 1:
            raise ValueError("ambiguous resource")
        observed = data[0]
        actual_name = observed["Name"].removeprefix("/")
        labels = (
            observed["Config"].get("Labels", {})
            if kind == "container"
            else observed.get("Labels", {})
        )
        if actual_name != name or labels.get("runspindle.synthetic") != "true":
            raise ValueError("owned resource identity drift")
        if kind != "volume" and not observed["Id"].startswith(identity):
            raise ValueError("owned resource ID drift")
        result.append((observed["Id"] if kind != "volume" else name, name))
    return result


def cleanup(run_id: str) -> None:
    deadline = time.monotonic() + 180
    # Stop owned workloads before volume/network teardown. No unrelated cleanup.
    for kind in ("container", "volume", "network"):
        for identity, _ in inventory(kind, run_id, deadline):
            docker(
                [kind, "rm", *(["--force"] if kind == "container" else []), identity],
                deadline,
            )
        if inventory(kind, run_id, deadline):
            raise ValueError("owned resource remains")


def run(prepared: Path, evidence: Path) -> int:
    run_id = uuid.uuid4().hex
    report: dict[str, Any] = {
        "run_id": run_id,
        "source_scope": "manual_synthetic_temporal_gate",
        "status": "failed",
        "cleanup": "unknown",
    }
    evidence.mkdir(mode=0o700)  # Exclusive new directory, never overwrite evidence.
    (evidence / "attempt.json").write_text(json.dumps(report) + "\n")
    deadline = time.monotonic() + 45
    for kind in ("container", "volume", "network"):
        if inventory(kind, run_id, deadline):
            raise ValueError("nonce already occupied")
    previous = {}
    child = None

    def interrupted(signum: int, frame: Any) -> None:
        raise InterruptedError("controller cancelled")

    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, interrupted)
        env = os.environ.copy()
        env.update(
            RUNSPINDLE_TEMPORAL_RUN_ID=run_id,
            RUNSPINDLE_TEMPORAL_RETAIN="0",
            PYTHONDONTWRITEBYTECODE="1",
        )
        with (evidence / "test.log").open("xb") as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "tools/python_tests.py"),
                    "--temporal",
                    "required",
                    "--prepared",
                    str(prepared),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            end = time.monotonic() + 900
            while child.poll() is None:
                if time.monotonic() >= end:
                    raise TimeoutError("test workload deadline")
                if log.tell() > 16 * 1024 * 1024:
                    raise ValueError("test log bound")
                time.sleep(0.2)
            code = child.returncode
        report["test_exit"] = code
        if code == 0:
            report["status"] = "passed"
    except BaseException as error:
        report["error_type"] = type(error).__name__
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
        except BaseException as error:
            report.update(status="failed", process_stop_error_type=type(error).__name__)
        try:
            cleanup(run_id)
            report["cleanup"] = (
                "uncertain_process"
                if "process_stop_error_type" in report
                else "verified"
            )
        except BaseException as error:
            report.update(status="failed", cleanup_error_type=type(error).__name__)
        finally:
            (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    print(json.dumps(report))
    return 0 if report["status"] == "passed" and report["cleanup"] == "verified" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    return run(args.prepared, args.evidence)


if __name__ == "__main__":
    raise SystemExit(main())
