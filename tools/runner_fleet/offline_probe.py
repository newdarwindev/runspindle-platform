"""Bounded, no-network guest tooling probe; never a CI qualification or promotion."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import resource
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

FILE_LIMIT = 512 * 1024**2
MIN_FREE = 34 * 1024**3  # 30 GiB reserve + four logs, disk, seed (3 GiB) + metadata.
MAX_IMAGE = 32 * 1024**3
COMMANDS = {
    "cargo": ["/opt/rust/bin/cargo", "--version"],
    "rustc": ["/opt/rust/bin/rustc", "--version"],
    "ruff": ["/opt/runspindle-quality/bin/ruff", "--version"],
}
EXPECTED = {"cargo": "cargo 1.94.1", "rustc": "rustc 1.94.1", "ruff": "ruff 0.14.5"}


def digest(path: Path, deadline: float) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            if time.monotonic() >= deadline:
                raise TimeoutError("Image verification deadline")
            value.update(block)
    return value.hexdigest()


def image_path(path: Path) -> Path:
    absolute = path.absolute()
    if absolute.resolve(strict=True) != absolute or any(
        c in str(absolute) for c in ",\n\r"
    ):
        raise ValueError("Image path must be canonical and contain no QEMU separators")
    info = absolute.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o222:
        raise ValueError("Image must be root-owned, regular, and sealed read-only")
    if not 0 < info.st_size <= MAX_IMAGE:
        raise ValueError("Image outside bounded size")
    return absolute


def parent_path(path: Path) -> Path:
    absolute = path.absolute()
    if absolute.resolve(strict=True) != absolute or any(
        c in str(absolute) for c in ",\n\r"
    ):
        raise ValueError("Evidence parent must be canonical")
    info = absolute.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("Evidence parent must be owned by this user with mode 0700")
    if {"system.posix_acl_access", "system.posix_acl_default"} & set(
        os.listxattr(absolute)
    ):
        raise ValueError("Evidence parent ACL is not supported")
    return absolute


def write(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def limits() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_LIMIT, FILE_LIMIT))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3, 4 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (240, 240))


def command(
    argv: list[str],
    directory: Path,
    name: str,
    deadline: float,
    seconds: int = 60,
    pass_fds: tuple[int, ...] = (),
) -> Path:
    """Keep complete bounded output; never truncate or delete on any outcome."""
    remaining = min(seconds, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("Probe deadline")
    path = directory / (name + ".log")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        supervised = [
            "/usr/bin/timeout",
            "--signal=TERM",
            "--kill-after=5s",
            str(remaining) + "s",
            *argv,
        ]
        process = subprocess.Popen(
            supervised,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
            preexec_fn=limits,
            pass_fds=pass_fds,
            env={
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C",
                "HOME": str(directory),
                "MALLOC_ARENA_MAX": "2",
            },
        )
        try:
            code = process.wait(timeout=remaining + 10)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        finally:
            output.flush()
            os.fsync(output.fileno())
    if code != 0:
        raise RuntimeError(f"{name} exited {code}; evidence retained")
    return path


def image_info(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 65536:
        raise ValueError("Image metadata exceeds bound")
    value: dict[str, Any] = json.loads(path.read_text())
    if (
        value.get("format") != "qcow2"
        or value.get("virtual-size", MAX_IMAGE + 1) > MAX_IMAGE
    ):
        raise ValueError("Unsupported image")
    if any(
        key in value
        for key in ("backing-filename", "full-backing-filename", "snapshots")
    ):
        raise ValueError("Image must be self-contained without snapshots")
    data = value.get("format-specific", {}).get("data", {})
    if any(str(key).replace("_", "-").startswith("data-file") for key in data):
        raise ValueError("External image data file")
    if value.get("dirty-flag") or data.get("corrupt"):
        raise ValueError("Image is dirty or corrupt")
    return value


def guest_config(nonce: str) -> bytes:
    script = """import json, subprocess
commands = COMMANDS
result = {}
for name, argv in commands.items():
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        result[name] = {"returncode": p.returncode, "version": p.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired):
        result[name] = {"returncode": -1, "version": "unavailable"}
with open('/dev/ttyS0', 'w') as serial:
    serial.write('MARKER' + json.dumps(result, sort_keys=True) + '\\n')
subprocess.run(['/sbin/poweroff'], check=False)
""".replace("COMMANDS", repr(COMMANDS)).replace(
        "MARKER", "RUNSPINDLE-OFFLINE-" + nonce + ":"
    )
    config = {
        "write_files": [
            {
                "path": "/run/runspindle-offline-probe.py",
                "permissions": "0600",
                "encoding": "b64",
                "content": base64.b64encode(script.encode()).decode(),
            }
        ],
        "runcmd": [["/usr/bin/python3", "/run/runspindle-offline-probe.py"]],
    }
    return b"#cloud-config\n" + json.dumps(config).encode() + b"\n"


def qemu_args(directory: Path) -> list[str]:
    return [
        "/usr/bin/qemu-system-x86_64",
        "-enable-kvm",
        "-machine",
        "q35,mem-merge=off",
        "-cpu",
        "host",
        "-smp",
        "2",
        "-m",
        "1024",
        "-nodefaults",
        "-no-user-config",
        "-display",
        "none",
        "-monitor",
        "none",
        "-nic",
        "none",
        "-no-reboot",
        "-drive",
        f"file={directory}/disk.qcow2,format=qcow2,if=virtio,cache=none",
        "-drive",
        f"file={directory}/seed.iso,format=raw,media=cdrom,readonly=on",
        "-serial",
        "stdio",
        "-sandbox",
        "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
    ]


def parse_result(
    path: Path, nonce: str, deadline: float | None = None
) -> dict[str, Any]:
    deadline = deadline if deadline is not None else time.monotonic() + 60
    if path.stat().st_size > FILE_LIMIT:
        raise ValueError("Guest capture exceeds bound")
    marker = ("RUNSPINDLE-OFFLINE-" + nonce + ":").encode()
    found = None
    with path.open("rb") as stream:
        while line := stream.readline(65537):
            if time.monotonic() >= deadline:
                raise TimeoutError("Guest parsing deadline")
            if len(line) > 65536:
                raise ValueError("Guest line exceeds bound")
            if line.startswith(marker):
                if found is not None:
                    raise ValueError("Repeated guest result")
                found = json.loads(line[len(marker) :])
    if not isinstance(found, dict) or set(found) != set(EXPECTED):
        raise ValueError("Missing, repeated, or invalid guest result")
    for name, expected in EXPECTED.items():
        item = found[name]
        version = item.get("version")
        if (
            item.get("returncode") != 0
            or not isinstance(version, str)
            or not (version == expected or version.startswith(expected + " ("))
        ):
            raise ValueError("Guest tool version mismatch: " + name)
    return found


def probe(image: Path, expected: str, parent: Path) -> dict[str, Any]:
    if not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError("Exact image SHA256 required")
    image, parent = image_path(image), parent_path(parent)
    deadline = time.monotonic() + 600
    if digest(image, deadline) != expected:
        raise ValueError("Image digest mismatch")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise RuntimeError("Existing KVM access required; no permission changes")
    # One process per evidence parent. No fleet database, credentials, or helpers.
    lock_fd = os.open(
        parent / "probe.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(lock_fd, "r+b") as lock:
        info = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("Unsafe probe lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        space = os.statvfs(parent)
        if space.f_bavail * space.f_frsize < MIN_FREE:
            raise RuntimeError("Need 34 GiB free including 30 GiB reserve")
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        if available < 11 * 1024**3:
            raise RuntimeError("Need 11 GiB memory available including host reserve")
        nonce = uuid.uuid4().hex
        directory = parent / nonce
        directory.mkdir(mode=0o700)
        result = {
            "schema_version": 1,
            "image_sha256": expected,
            "evidence_class": "offline_guest_tool_probe",
            "ci_qualified": False,
            "network_tested": False,
            "promoted": False,
            "status": "failed",
            "evidence_directory": str(directory),
        }
        try:
            image_info(
                command(
                    [
                        "/usr/bin/qemu-img",
                        "info",
                        "--output=json",
                        "-f",
                        "qcow2",
                        str(image),
                    ],
                    directory,
                    "image-info",
                    deadline,
                    pass_fds=(lock.fileno(),),
                )
            )
            command(
                [
                    "/usr/bin/qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-F",
                    "qcow2",
                    "-b",
                    str(image),
                    str(directory / "disk.qcow2"),
                ],
                directory,
                "create",
                deadline,
                pass_fds=(lock.fileno(),),
            )
            write(directory / "user-data", guest_config(nonce))
            write(
                directory / "meta-data",
                json.dumps({"instance-id": nonce, "dsmode": "local"}).encode(),
            )
            write(directory / "network-config", b'{"version":2,"ethernets":{}}\n')
            command(
                [
                    "/usr/bin/genisoimage",
                    "-output",
                    str(directory / "seed.iso"),
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-rock",
                    str(directory / "user-data"),
                    str(directory / "meta-data"),
                    str(directory / "network-config"),
                ],
                directory,
                "seed",
                deadline,
                pass_fds=(lock.fileno(),),
            )
            output = command(
                qemu_args(directory),
                directory,
                "guest",
                deadline,
                240,
                (lock.fileno(),),
            )
            result["versions"] = parse_result(output, nonce, deadline)
            if digest(image, deadline) != expected:
                raise ValueError("Base image changed during probe")
            result["status"] = "passed"
        except (Exception, KeyboardInterrupt) as error:
            result["error"] = type(error).__name__ + ": " + str(error)
        finally:
            write(
                directory / "result.json",
                (json.dumps(result, sort_keys=True, indent=2) + "\n").encode(),
            )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--evidence-parent", type=Path, required=True)
    args = parser.parse_args()

    def terminated(signum: int, frame: object) -> None:
        raise InterruptedError("Probe terminated")

    signal.signal(signal.SIGTERM, terminated)
    result = probe(args.image, args.sha256, args.evidence_parent)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
