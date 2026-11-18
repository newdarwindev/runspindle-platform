"""One unregistered guest observation; never queue activation or image promotion.

Execution requires separate owner approval for the existing host network helper.
The default path verifies local identities only and performs no fleet mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from .backend import Backend, execute, helper
from .candidate import time_budget
from .offline_probe import digest, image_path, write
from .state import State

STATE_DIRECTORY = Path("/home/standard/.local/state/runspindle-fleet")
DEPLOYED_MANAGER = Path("/opt/runspindle-fleet/tools/runner_fleet/manager.py")
DEPLOYED_HELPER = Path("/opt/runspindle-fleet/tools/runner_fleet/host_helper.py")
EXPECTED_HELPER_SHA256 = (
    "ed132338f8895bf31ebccba50db99a06d498c5037a2d1fb43f5d2deccee876b9"
)
EXPECTED_MANAGER_SHA256 = (
    "6a4439221e29a2decc51737d88fe9821a4aeb7efa29a86330b70cbd7654abe51"
)

FIELDS = (
    "image_sha256",
    "image_source_sha",
    "controller_sha256",
    "recipe_sha256",
    "tool_closure_sha256",
)
GUEST_SCRIPT = """python3 - <<'PROBE'
import hashlib, json, pathlib, subprocess
instance=json.loads(pathlib.Path('/etc/runspindle-instance.json').read_text())
image=json.loads(pathlib.Path('/etc/runspindle-image.json').read_text())
versions={}
for name,cmd in [('cargo','/opt/rust/bin/cargo'),('rustc','/opt/rust/bin/rustc'),('ruff','/opt/runspindle-quality/bin/ruff')]:
 p=subprocess.run([cmd,'--version'],capture_output=True,text=True,timeout=15)
 versions[name]={'code':p.returncode,'version':p.stdout.strip()}
print(json.dumps({'instance':{k:instance.get(k) for k in ('resource','slot','image_sha256','image_source_sha','controller_sha256','recipe_sha256','tool_closure_sha256')},'versions':versions,'rust_closure_sha256':image.get('rust_closure_sha256'),'builder_archive_sha256':image.get('runtime_builder',{}).get('archive_sha256'),'quality_closure_sha256':hashlib.sha256(pathlib.Path('/opt/runspindle-image-tools/fleet/quality-closure.json').read_bytes()).hexdigest()}))
PROBE
"""


def read_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("Invalid manifest file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Manifest exceeded limit")
        return raw
    finally:
        os.close(fd)


def read_record(path: Path) -> dict[str, Any]:
    value = json.loads(read_regular(path))
    if not isinstance(value, dict):
        raise ValueError("Manifest must be object")
    return value


def plan(image: Path, manifest: Path, expected: Path) -> dict[str, Any]:
    image = image_path(image)
    pin = read_record(expected)["image"]
    actual = digest(image, time.monotonic() + 120)
    recorded = read_record(manifest)
    if recorded.get("sha256") != actual or recorded.get("candidate_path") != str(image):
        raise ValueError("Candidate differs from recorded manifest")
    values = {
        "image_sha256": actual,
        "image_source_sha": recorded.get("source_sha"),
        **{k: recorded.get(k) for k in FIELDS[2:]},
    }
    for key, value in values.items():
        width = 40 if key == "image_source_sha" else 64
        if not isinstance(value, str) or not re.fullmatch(
            "[0-9a-f]{%d}" % width, value
        ):
            raise ValueError("Missing exact candidate binding")
    wanted = {
        "image_sha256": pin["sha256"],
        "image_source_sha": pin["source_sha"],
        **{k: pin[k] for k in FIELDS[2:]},
    }
    if values != wanted:
        raise ValueError("Candidate differs from repository image pins")
    return {
        "bindings": values,
        "image": str(image),
        "manifest": recorded,
        "rust_closure_sha256": pin["rust_closure_sha256"],
        "builder_archive_sha256": pin["builder_archive_sha256"],
        "quality_closure_sha256": pin["quality_closure_sha256"],
    }


def validate_observation(
    observed: dict[str, Any], intended: dict[str, Any], resource: dict[str, Any]
) -> None:
    expected = intended["bindings"] | {
        "resource": resource["id"],
        "slot": resource["slot"],
    }
    if observed.get("instance") != expected:
        raise ValueError("Guest instance attestation mismatch")
    for key in (
        "rust_closure_sha256",
        "builder_archive_sha256",
        "quality_closure_sha256",
    ):
        if observed.get(key) != intended[key]:
            raise ValueError("Guest dependency closure mismatch")
    for name, version in (
        ("cargo", "cargo 1.94.1"),
        ("rustc", "rustc 1.94.1"),
        ("ruff", "ruff 0.14.5"),
    ):
        item = observed.get("versions", {}).get(name, {})
        value = item.get("version")
        if (
            item.get("code") != 0
            or not isinstance(value, str)
            or not (value == version or value.startswith(version + " ("))
        ):
            raise ValueError("Guest tool version mismatch")


class IsolatedMetadata:
    """Use shared capacity database but never overwrite its candidate metadata."""

    def __init__(self, state: State, directory: Path):
        self.directory = directory
        self._state = state

    def update(self, resource: dict[str, Any], phase: str, **details: Any) -> None:
        self._state.update(resource, phase, **details)


def manager_stopped() -> None:
    value = execute(
        [
            "systemctl",
            "show",
            "runspindle-fleet.service",
            "--property=ActiveState",
            "--value",
        ]
    ).stdout.strip()
    if value != "inactive":
        raise ValueError("Manager must already be inactive")


def verify_deployed_guard() -> None:
    for path in (DEPLOYED_MANAGER, Path(__file__).with_name("manager.py")):
        if hashlib.sha256(read_regular(path)).hexdigest() != EXPECTED_MANAGER_SHA256:
            raise ValueError("Reviewed probe recovery guard is not installed")
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
        raise ValueError("Unexpected fleet service command")
    argv = shlex.split(unit.split("argv[]=", 1)[1].split(";", 1)[0].strip())
    if argv != [
        "/usr/bin/python3",
        "/opt/runspindle-fleet/tools/fleet.py",
        "manage",
        "--host",
        "strix",
        "--state-dir",
        str(STATE_DIRECTORY),
    ]:
        raise ValueError("Unexpected fleet service command")


def verify_host_lifetime_policy() -> None:
    # Existing transient host unit stops the guest even if this process is killed.
    if (
        hashlib.sha256(read_regular(DEPLOYED_HELPER)).hexdigest()
        != EXPECTED_HELPER_SHA256
    ):
        raise ValueError("Unreviewed host lifetime policy")


def verify_guest_lifetime(slot: int) -> None:
    value = execute(
        [
            "systemctl",
            "show",
            f"runspindle-vm-{slot}.service",
            "--property=RuntimeMaxUSec,TimeoutStopUSec,KillMode",
        ]
    ).stdout
    fields = dict(line.split("=", 1) for line in value.splitlines() if "=" in line)
    if fields != {
        "RuntimeMaxUSec": "25min",
        "TimeoutStopUSec": "20s",
        "KillMode": "control-group",
    }:
        raise ValueError("Independent guest lifetime bound unavailable")


def validate_state_directory(directory: Path) -> None:
    if directory != STATE_DIRECTORY or directory.resolve(strict=True) != directory:
        raise ValueError("Canonical installed shared fleet state required")
    info = directory.stat()
    database = directory / "fleet.sqlite"
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Unsafe shared state directory")
    if database.is_symlink() or not stat.S_ISREG(database.lstat().st_mode):
        raise ValueError("Existing shared capacity database required")


def cleanup(
    state: State, backend: Any, resource: dict[str, Any], report: dict[str, Any]
) -> None:
    # Stopping the owned guest cannot depend on successful DB reads/updates.
    errors = []
    try:
        with time_budget(15):
            state.update(resource, "cleaning")
    except BaseException as error:
        errors.append(type(error).__name__)
    try:
        with time_budget(180):
            backend.destroy(resource)
            status = helper("observe", resource["slot"])
            if status.get("ActiveState") not in ("inactive", "failed"):
                raise ValueError("Guest stop unverified")
    except BaseException as error:
        errors.append(type(error).__name__)
    try:
        with time_budget(15):
            if errors:
                state.update(resource, "uncertain", cleanup_error=",".join(errors))
            else:
                state.update(resource, "destroyed", cleanup_verified=True)
    except BaseException as error:
        errors.append(type(error).__name__)
    if errors:
        report.update(status="failed", cleanup="blocked", cleanup_error_types=errors)
    else:
        report["cleanup"] = "verified"


def recover(intended: dict[str, Any], state: State, identity: str) -> dict[str, Any]:
    validate_state_directory(state.directory)
    manager_stopped()
    with state.lock():
        manager_stopped()
        resources = [r for r in state.active() if r["id"] == identity]
        if len(resources) != 1:
            raise ValueError("Exact occupied probe reservation required")
        resource = resources[0]
        details = resource["details"]
        name = details.get("probe_directory", "")
        if (
            details.get("unregistered_probe") is not True
            or not isinstance(name, str)
            or not re.fullmatch("single-probe-[0-9a-f]{32}", name)
            or details.get("probe_image") != intended["image"]
            or details.get("image_sha256") != intended["bindings"]["image_sha256"]
        ):
            raise ValueError("Probe recovery binding mismatch")
        directory = state.directory / name
        if (
            directory.resolve(strict=True) != directory
            or read_record(directory / "candidate.json") != intended["manifest"]
        ):
            raise ValueError("Probe recovery metadata mismatch")
        backend: Any = Backend(IsolatedMetadata(state, directory), intended["image"])
        report = {
            "status": "passed",
            "resource": identity,
            "ci_qualified": False,
            "promoted": False,
            "registered": False,
            "recovery_only": True,
        }
        cleanup(state, backend, resource, report)
        write(
            directory / ("recovery-" + uuid.uuid4().hex + ".json"),
            (json.dumps(report) + "\n").encode(),
        )
        return report


def terminated(signum: int, frame: Any) -> None:
    raise InterruptedError("Probe interrupted")


def run(intended: dict[str, Any], state: State) -> dict[str, Any]:
    validate_state_directory(state.directory)
    manager_stopped()
    with state.lock():
        manager_stopped()
        if state.active():
            raise ValueError("All shared slots must already be reconciled empty")
        directory = state.directory / ("single-probe-" + uuid.uuid4().hex)
        directory.mkdir(mode=0o700)
        write(
            directory / "candidate.json",
            (json.dumps(intended["manifest"]) + "\n").encode(),
        )
        resource = state.reserve(
            {"repository": "runspindle-platform", "id": -1},
            details={
                "unregistered_probe": True,
                "probe_directory": directory.name,
                "probe_image": intended["image"],
                "image_sha256": intended["bindings"]["image_sha256"],
            },
        )
        # Existing lifecycle adapter uses dynamic JSON records.
        backend: Any = Backend(IsolatedMetadata(state, directory), intended["image"])
        report: dict[str, Any] = {
            "status": "failed",
            "ci_qualified": False,
            "promoted": False,
            "registered": False,
            "resource": resource["id"],
            "bindings": intended["bindings"],
        }
        try:
            with time_budget(360):
                backend.create(resource)
                verify_guest_lifetime(resource["slot"])
                deadline = time.monotonic() + 240
                while not backend.ready(resource):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Guest startup deadline")
                    time.sleep(2)
                response = backend.ssh(resource, GUEST_SCRIPT, timeout=60)
                observed = json.loads(response.stdout)
                validate_observation(observed, intended, resource)
                report.update(status="passed", observation=observed)
        except BaseException as error:
            report["error_type"] = type(error).__name__
        finally:
            cleanup(state, backend, resource, report)
            write(
                directory / "result.json",
                (json.dumps(report, indent=2) + "\n").encode(),
            )
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "manifest", "expected", "state-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--recover", help="Exact prior probe resource; cleanup only")
    args = parser.parse_args()
    intended = plan(args.image, args.manifest, args.expected)
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "identity_verified_only",
                    "bindings": intended["bindings"],
                    "network_actions": "not_run",
                    "ci_qualified": False,
                }
            )
        )
        return 0
    validate_state_directory(args.state_dir)
    verify_deployed_guard()
    verify_host_lifetime_policy()
    previous = signal.signal(signal.SIGTERM, terminated)
    state: State | None = None
    try:
        state = State(args.state_dir)
        result = (
            recover(intended, state, args.recover)
            if args.recover
            else run(intended, state)
        )
    finally:
        try:
            if state is not None:
                state.db.close()
        finally:
            signal.signal(signal.SIGTERM, previous)
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
