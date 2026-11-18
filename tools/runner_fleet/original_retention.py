"""No-copy retention primitive; source-only, not wired into the manager.

Caller must hold canonical capacity lock, verify the owned guest stopped and the
registration absent, and persist intent/result in the existing resource record.
No capacity release happens here. On any failure keep reservation occupied and
reconcile exact paths manually; never auto-delete, recopy or retry. The trusted
host operator exclusively owns these private paths (not hostile same-UID safety).
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable

from .retention import file_digest, sync_directory

MAX_DISK = 24 * 1024**3


def _stamp(path: Path, limit: int) -> dict[str, int]:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Retention path must be canonical without links")
    value = path.lstat()
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or value.st_size > limit
        or value.st_uid != os.getuid()
        or value.st_mode & 0o077
    ):
        raise ValueError("Unsafe retention input")
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "mode": stat.S_IMODE(value.st_mode),
    }


def _private(path: Path) -> None:
    value = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o700
        or {"system.posix_acl_access", "system.posix_acl_default"}
        & set(os.listxattr(path))
    ):
        raise ValueError("Unsafe retention directory")


def inspect_linked_disk(
    disk: Path,
    base: Path,
    expected_base: str,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    """Read-only qemu metadata and byte binding. No convert/check-repair/copy."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_base):
        raise ValueError("Missing base binding")
    disk_before = _stamp(disk, MAX_DISK + 1024**3)
    # Base may be root-owned, but must be immutable to ordinary writers.
    if not base.is_absolute() or base.resolve() != base:
        raise ValueError("Linked base")
    before = base.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_mode & 0o222
        or before.st_size > MAX_DISK + 1024**3
    ):
        raise ValueError("Unsealed base")
    base_sha = file_digest(base)
    if base_sha != expected_base:
        raise ValueError("Backing bytes differ")

    def info(path: Path) -> dict[str, Any]:
        result = run(["qemu-img", "info", "--output=json", "-f", "qcow2", str(path)])
        if result.returncode or len(result.stdout.encode()) > 16384:
            raise ValueError("Unbounded or failed image metadata")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("format") != "qcow2":
            raise ValueError("Unexpected image format")
        details = value.get("format-specific", {}).get("data")
        if not isinstance(details, dict) or any(
            "data-file" in str(k).replace("_", "-") or "encrypt" in str(k)
            for k in details
        ):
            raise ValueError("Unsupported external/encrypted image")
        return value

    source, backing = info(disk), info(base)
    size, base_size = source.get("virtual-size"), backing.get("virtual-size")
    if (
        type(size) is not int
        or type(base_size) is not int
        or not 0 < base_size <= size <= MAX_DISK
        or source.get("full-backing-filename") != str(base)
        or source.get("backing-filename") != str(base)
        or source.get("backing-filename-format") != "qcow2"
        or any(k in backing for k in ("backing-filename", "full-backing-filename"))
    ):
        raise ValueError("Unexpected backing chain")
    disk_sha = file_digest(disk)
    after = base.lstat()
    if (
        disk_before != _stamp(disk, MAX_DISK + 1024**3)
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or file_digest(base) != expected_base
    ):
        raise ValueError("Retention inputs changed")
    return {
        "base_path": str(base),
        "base_sha256": base_sha,
        "base_device": after.st_dev,
        "base_inode": after.st_ino,
        "overlay_sha256": disk_sha,
        "virtual_bytes": size,
        "requires_exact_base": True,
    }


def verify_backing_binding(binding: dict[str, Any]) -> None:
    """Revalidate retained base bytes and file identity, never repair or restore."""
    base = Path(binding["base_path"])
    if not base.is_absolute() or base.resolve() != base:
        raise ValueError("Retained backing path changed")
    value = base.lstat()
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
        or value.st_mode & 0o222
        or value.st_dev != binding["base_device"]
        or value.st_ino != binding["base_inode"]
        or file_digest(base) != binding["base_sha256"]
    ):
        raise ValueError("Retained backing identity changed")


def retain_originals(
    files: dict[str, Path],
    archive: Path,
    binding: dict[str, Any],
    persist: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Move only original inodes, recording intent before the first move.

    No new data copies/backups. Existing files are preserved on every failure;
    interrupted multi-file moves require explicit recovery from persisted intent.
    Caller passes a just-verified inspect_linked_disk binding and rechecks the
    exact base before releasing capacity. This function never releases it.
    """
    if set(files) != {"disk", "seed", "serial", "key"}:
        raise ValueError("Incomplete original file set")
    _private(archive)
    limits = {
        "disk": MAX_DISK + 1024**3,
        "seed": 16 * 1024**2,
        "serial": 64 * 1024**2,
        "key": 16384,
    }
    rows: dict[str, dict[str, Any]] = {}
    for name, path in files.items():
        _private(path.parent)
        stamp = _stamp(path, limits[name])
        destination = archive / (name + "-original")
        if (
            destination.exists()
            or destination.is_symlink()
            or path.stat().st_dev != archive.stat().st_dev
        ):
            raise ValueError("Occupied or cross-device retention destination")
        rows[name] = {
            "source": str(path),
            "destination": str(destination),
            "stamp": stamp,
            "sha256": file_digest(path),
        }
    if (
        rows["disk"]["sha256"] != binding.get("overlay_sha256")
        or binding.get("requires_exact_base") is not True
    ):
        raise ValueError("Overlay inspection binding differs")
    verify_backing_binding(binding)
    receipt = {"schema": "original-retention-v1", "files": rows, "backing": binding}
    persist("intent", copy.deepcopy(receipt))
    for name, row in rows.items():
        source, destination = Path(row["source"]), Path(row["destination"])
        if (
            _stamp(source, limits[name]) != row["stamp"]
            or file_digest(source) != row["sha256"]
        ):
            raise ValueError("Original changed after retention intent")
        if destination.exists() or destination.is_symlink():
            raise ValueError("Destination became occupied")
        source.rename(destination)
        if (
            _stamp(destination, limits[name]) != row["stamp"]
            or file_digest(destination) != row["sha256"]
        ):
            raise ValueError("Original move identity differs")
        sync_directory(source.parent)
        sync_directory(archive)
    verify_backing_binding(binding)
    persist("complete", copy.deepcopy(receipt))
    return receipt


def resume_originals(
    files: dict[str, Path],
    archive: Path,
    receipt: dict[str, Any],
    persist: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Explicit recovery of an already durable intent, never a new retention plan.

    Verify ALL original identities before moving any remainder. Exactly one of
    each source/destination may exist. A missing, duplicated, changed or aliased
    input keeps capacity occupied; no copy, deletion, substitution or repair.
    """
    if (
        type(receipt) is not dict
        or set(receipt) != {"schema", "files", "backing"}
        or receipt["schema"] != "original-retention-v1"
        or set(files) != {"disk", "seed", "serial", "key"}
        or type(receipt["files"]) is not dict
        or set(receipt["files"]) != set(files)
    ):
        raise ValueError("Unknown retention recovery schema")
    _private(archive)
    verify_backing_binding(receipt["backing"])
    limits = {
        "disk": MAX_DISK + 1024**3,
        "seed": 16 * 1024**2,
        "serial": 64 * 1024**2,
        "key": 16384,
    }
    pending = []
    for name, source in files.items():
        destination = archive / (name + "-original")
        row = receipt["files"][name]
        _private(source.parent)
        if (
            type(row) is not dict
            or set(row) != {"source", "destination", "stamp", "sha256"}
            or row["source"] != str(source)
            or row["destination"] != str(destination)
            or source.is_symlink()
            or destination.is_symlink()
        ):
            raise ValueError("Recovery path binding differs")
        source_exists, destination_exists = source.exists(), destination.exists()
        if source_exists == destination_exists:
            raise ValueError("Recovery requires exactly one original inode")
        observed = source if source_exists else destination
        if (
            _stamp(observed, limits[name]) != row["stamp"]
            or file_digest(observed) != row["sha256"]
            or observed.stat().st_dev != archive.stat().st_dev
        ):
            raise ValueError("Recovery original identity differs")
        if source_exists:
            pending.append((name, source, destination, row))
    if receipt["files"]["disk"]["sha256"] != receipt["backing"]["overlay_sha256"]:
        raise ValueError("Recovery overlay binding differs")
    # Caller has exclusive canonical lock and private directories; no hostile
    # same-UID concurrent writer guarantee is asserted.
    for name, source, destination, row in pending:
        if (
            _stamp(source, limits[name]) != row["stamp"]
            or file_digest(source) != row["sha256"]
            or destination.exists()
            or destination.is_symlink()
        ):
            raise ValueError("Recovery changed before move")
        source.rename(destination)
        if (
            _stamp(destination, limits[name]) != row["stamp"]
            or file_digest(destination) != row["sha256"]
        ):
            raise ValueError("Recovered original differs")
        sync_directory(source.parent)
        sync_directory(archive)
    verify_backing_binding(receipt["backing"])
    persist("complete", copy.deepcopy(receipt))
    return receipt
