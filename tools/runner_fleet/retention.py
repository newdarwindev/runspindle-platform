"""Retain diagnostic captures without replacing earlier evidence."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import uuid
from pathlib import Path
from typing import BinaryIO, Callable


def _capture(directory: Path, name: str) -> tuple[Path, BinaryIO]:
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("Invalid diagnostic capture name")
    path = directory / name
    while True:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            return path, os.fdopen(descriptor, "wb")
        except FileExistsError:
            path = directory / f"{name}.capture-{uuid.uuid4().hex}"


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def capture_bytes(directory: Path, name: str, payload: bytes) -> Path:
    path, output = _capture(directory, name)
    with output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(payload).digest():
        raise RuntimeError("Diagnostic capture verification failed; evidence retained")
    sync_directory(directory)
    return path


def capture_file(source: Path, directory: Path, name: str) -> Path:
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as original:
        info = os.fstat(original.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Diagnostic source is not a regular file")
        path, output = _capture(directory, name)
        expected = hashlib.sha256()
        remaining = info.st_size
        with output:
            # A running log may grow. Capture its observed prefix; final stopped
            # logs are separately checked in full before relocating the original.
            while remaining:
                chunk = original.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeError("Diagnostic source shortened; capture retained")
                output.write(chunk)
                expected.update(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
    observed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            observed.update(chunk)
    if observed.digest() != expected.digest():
        raise RuntimeError("Diagnostic capture verification failed; evidence retained")
    sync_directory(directory)
    return path


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError("Retention input is not a regular file")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def snapshot_guest_disk(
    disk: Path,
    base: Path,
    directory: Path,
    expected_base: str,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]],
    maximum_size: int = 24 * 1024**3,
) -> dict[str, str | int]:
    """Create and compare a self-contained copy before moving the original.

    The callback must not raise for nonzero exit: every command's complete result
    is retained here before failure is propagated. No source or base is rewritten.
    """
    generation = directory / f"guest-retention-{uuid.uuid4().hex}"
    generation.mkdir(mode=0o700)
    sync_directory(directory)

    def command(argv: list[str]) -> str:
        try:
            result = run(argv)
        except (subprocess.TimeoutExpired, OSError) as error:

            def output(value: bytes | str | None) -> str:
                return (
                    value.decode(errors="replace")
                    if isinstance(value, bytes)
                    else value or ""
                )

            capture_bytes(
                generation,
                "commands.log",
                json.dumps(
                    {
                        "argv": argv,
                        "status": "timeout_or_os_error",
                        "error": str(error),
                        "stdout": output(error.stdout)
                        if isinstance(error, subprocess.TimeoutExpired)
                        else "",
                        "stderr": output(error.stderr)
                        if isinstance(error, subprocess.TimeoutExpired)
                        else "",
                    },
                    sort_keys=True,
                ).encode(),
            )
            raise
        capture_bytes(
            generation,
            "commands.log",
            json.dumps(
                {
                    "argv": argv,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                sort_keys=True,
            ).encode(),
        )
        if result.returncode != 0:
            raise RuntimeError("Guest retention command failed; all outputs retained")
        return result.stdout

    def info(path: Path) -> dict[str, object]:
        value = json.loads(
            command(["qemu-img", "info", "--output=json", "-f", "qcow2", str(path)])
        )
        if not isinstance(value, dict) or value.get("format") != "qcow2":
            raise RuntimeError("Invalid guest retention image metadata")
        specific = value.get("format-specific")
        if not isinstance(specific, dict) or not isinstance(specific.get("data"), dict):
            raise RuntimeError("Missing qcow2 format details")
        if any(
            str(key).replace("_", "-").startswith("data-file")
            for key in specific["data"]
        ):
            raise RuntimeError("External data-file dependency refused")
        return value

    def stamp(path: Path) -> tuple[int, int, int, int, int]:
        observed = path.lstat()
        if not stat.S_ISREG(observed.st_mode):
            raise RuntimeError("Linked or nonregular guest retention image")
        return (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )

    original_stamp, base_stamp = stamp(disk), stamp(base)
    original_sha = file_digest(disk)
    if file_digest(base) != expected_base:
        raise RuntimeError("Guest backing bytes differ from reserved image")
    source_info, base_info = info(disk), info(base)
    virtual_size = source_info.get("virtual-size")
    base_size = base_info.get("virtual-size")
    if (
        source_info.get("full-backing-filename") != str(base.absolute())
        or source_info.get("backing-filename-format") != "qcow2"
        or any(
            key in base_info for key in ("backing-filename", "full-backing-filename")
        )
        or type(virtual_size) is not int
        or type(base_size) is not int
        or virtual_size <= 0
        or base_size <= 0
        or base_size > virtual_size
        or virtual_size > maximum_size
    ):
        raise RuntimeError("Unexpected guest backing chain; originals retained")
    snapshot = generation / "standalone.qcow2"
    command(["qemu-img", "check", "-q", "-f", "qcow2", str(disk)])
    command(
        ["qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", str(disk), str(snapshot)]
    )
    snapshot_info = info(snapshot)
    if (
        any(
            key in snapshot_info
            for key in ("backing-filename", "full-backing-filename")
        )
        or snapshot_info.get("virtual-size") != virtual_size
    ):
        raise RuntimeError("Guest retention copy is not self-contained")
    command(
        ["qemu-img", "compare", "-f", "qcow2", "-F", "qcow2", str(disk), str(snapshot)]
    )
    if (
        stamp(disk) != original_stamp
        or stamp(base) != base_stamp
        or file_digest(disk) != original_sha
        or file_digest(base) != expected_base
    ):
        raise RuntimeError("Guest retention inputs changed; originals retained")
    with snapshot.open("rb") as stream:
        os.fsync(stream.fileno())
    snapshot.chmod(0o400)
    snapshot_sha = file_digest(snapshot)
    receipt: dict[str, str | int] = {
        "standalone": str(snapshot),
        "standalone_sha256": snapshot_sha,
        "original_overlay_sha256": original_sha,
        "observed_backing_image": str(base),
        "observed_backing_image_sha256": expected_base,
        "virtual_size": virtual_size,
        "logical_compare": "equal",
        "backing_dependency": "none_for_standalone_copy",
        "scope": "persisted virtual-disk bytes; not guest/live/tool qualification",
    }
    capture_bytes(
        generation,
        "verified-snapshot.json",
        json.dumps(receipt, sort_keys=True).encode(),
    )
    original = generation / "original-overlay.qcow2"
    disk.rename(original)
    sync_directory(generation)
    sync_directory(disk.parent)
    if file_digest(original) != original_sha:
        raise RuntimeError("Relocated original guest bytes changed; evidence retained")
    return receipt
