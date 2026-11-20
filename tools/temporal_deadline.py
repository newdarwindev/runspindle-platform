#!/usr/bin/env python3
"""Explicit approved-test supervisor; not a service or automatic admission path.

Source-only until reviewed and separately approved for Docker bridge effects.
900s test budget +30s graceful stop +10s kill wait +190s owned cleanup (180s calls plus final CLI termination). Prefix
and synthetic label must both match. Unknown identities retain evidence and fail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
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


def leader_status(child: subprocess.Popen[bytes]) -> int | None:
    """Observe without reaping: the PID/group cannot be reused during cleanup."""
    status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if status is None:
        return None
    return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status


def active_group(group: int) -> list[int]:
    found = []
    with os.scandir("/proc") as entries:
        for count, entry in enumerate(entries):
            if count > 16384:
                raise ValueError("process inventory bound")
            if not entry.name.isdigit():
                continue
            try:
                raw = (Path(entry.path) / "stat").read_text()
            except FileNotFoundError:
                continue
            fields = raw.rsplit(")", 1)[1].split()
            if int(fields[2]) == group and fields[0] not in ("Z", "X"):
                found.append(int(entry.name))
    return found


def stop_group(child: subprocess.Popen[bytes], grace: float = 30) -> None:
    # Caller MUST NOT poll()/wait() first. The unreaped leader pins this group ID.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace
    while active_group(child.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 10
    child.wait(timeout=10)  # Reap only after the final group signal.
    while active_group(child.pid):
        if time.monotonic() >= deadline:
            raise ValueError("process group termination uncertain")
        time.sleep(0.05)


def capture(
    child: subprocess.Popen[bytes], deadline: float, limit: int, output: Any
) -> int:
    if child.stdout is None:
        raise ValueError("bounded pipe required")
    count = 0
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("bounded process deadline")
            events = selector.select(0.05)
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    count += len(chunk)
                    if count > limit:
                        raise ValueError("output bound")
                    output.write(chunk)
            status = leader_status(child)
            # Drain already available buffered output even if the leader exited.
            if status is not None and not selector.select(0):
                return status


def docker(args: list[str], deadline: float) -> str:
    import io

    end = min(deadline, time.monotonic() + 15)
    if end <= time.monotonic():
        raise TimeoutError("cleanup deadline")
    child = subprocess.Popen(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    output = io.BytesIO()
    try:
        code = capture(child, end, 128 * 1024, output)
        if code != 0:
            raise RuntimeError("Docker observation failed")
        return output.getvalue().decode()
    finally:
        stop_group(child, grace=0)
        if child.stdout is not None:
            child.stdout.close()


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
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            code = capture(child, time.monotonic() + 900, 16 * 1024 * 1024, log)
        report["test_exit"] = code
        if code == 0:
            report["status"] = "passed"
    except BaseException as error:
        report["error_type"] = type(error).__name__
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if child is not None:
                stop_group(child)
                if child.stdout is not None:
                    child.stdout.close()
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
