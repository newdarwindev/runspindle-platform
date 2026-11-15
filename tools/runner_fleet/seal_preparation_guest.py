#!/usr/bin/env python3
"""Guest-only sealing preparation; preserve originals before identity cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

MARKER = Path("/etc/runspindle-instance.json")
MACHINE_ID = Path("/etc/machine-id")
EVIDENCE = Path("/var/lib/runspindle-preparation-evidence")


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def file_hash(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("nonregular sealing input refused")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        os.fsync(stream.fileno())
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError("sealing input changed while hashing")
    return digest.hexdigest()


def tree_inventory(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def visit(path: Path, relative: str) -> None:
        info = path.lstat()
        row: dict[str, Any] = {
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "device": info.st_dev,
            "inode": info.st_ino,
        }
        if stat.S_ISLNK(info.st_mode):
            row.update(kind="symlink", target=os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            row["kind"] = "directory"
            for child in sorted(path.iterdir()):
                visit(
                    child,
                    child.name if relative == "." else relative + "/" + child.name,
                )
        elif stat.S_ISREG(info.st_mode):
            row.update(kind="file", bytes=info.st_size, sha256=file_hash(path))
        else:
            raise RuntimeError("unsupported cloud metadata object retained in place")
        result[relative] = row

    visit(root, ".")
    return result


def private_capture(directory: Path, name: str, payload: bytes) -> Path:
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("unsafe preparation evidence name")
    path = directory / name
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if path.read_bytes() != payload:
        raise RuntimeError(
            "preparation evidence verification failed; originals retained"
        )
    sync_directory(directory)
    return path


def archive_cloud_metadata(source: Path, generation: Path) -> dict[str, object]:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError("regular cloud metadata directory required")
    before = tree_inventory(source)
    private_capture(
        generation, "cloud-before.json", json.dumps(before, sort_keys=True).encode()
    )
    if source.stat().st_dev != generation.stat().st_dev:
        raise RuntimeError("cloud retention crosses filesystems; no fallback")
    destination = generation / "cloud-original"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("cloud retention destination occupied")
    source.rename(destination)
    sync_directory(source.parent)
    sync_directory(generation)
    after = tree_inventory(destination)
    private_capture(
        generation, "cloud-after.json", json.dumps(after, sort_keys=True).encode()
    )
    if after != before:
        raise RuntimeError(
            "cloud metadata changed; retained original must be reconciled"
        )
    source.mkdir(mode=before["."]["mode"])
    sync_directory(source.parent)
    if source.is_symlink() or any(source.iterdir()):
        raise RuntimeError("fresh cloud metadata directory is not empty")
    return {
        "original": str(destination),
        "entries": len(after),
        "verified_same_inodes_and_bytes": True,
        "fresh_directory": str(source),
    }


def archive_debug_tree(source: Path, generation: Path) -> dict[str, object]:
    if source.is_symlink() or not source.is_dir():
        raise RuntimeError("regular debug directory required; no log deletion")
    if source.stat().st_dev != generation.stat().st_dev:
        raise RuntimeError("debug retention crosses filesystems; fail before sealing")
    before = tree_inventory(source)
    private_capture(
        generation, "debug-before.json", json.dumps(before, sort_keys=True).encode()
    )
    destination = generation / "debugbuild-original"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("debug retention destination occupied")
    source.rename(destination)
    sync_directory(source.parent)
    sync_directory(generation)
    after = tree_inventory(destination)
    private_capture(
        generation, "debug-after.json", json.dumps(after, sort_keys=True).encode()
    )
    if before != after:
        raise RuntimeError(
            "debug data changed; retained originals require reconciliation"
        )
    return {
        "original": str(destination),
        "entries": len(after),
        "verified_same_inodes_and_bytes": True,
    }


def archive_identity_file(
    source: Path, generation: Path, name: str
) -> dict[str, object]:
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("linked or nonregular guest identity input refused")
    expected = file_hash(source)
    if info.st_dev != generation.stat().st_dev:
        raise RuntimeError("guest identity retention crosses filesystems; no fallback")
    destination = generation / name
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("guest identity retention destination occupied")
    source.rename(destination)
    sync_directory(source.parent)
    sync_directory(generation)
    observed = destination.lstat()
    if (observed.st_dev, observed.st_ino) != (info.st_dev, info.st_ino) or file_hash(
        destination
    ) != expected:
        raise RuntimeError(
            "guest identity changed; originals retained for reconciliation"
        )
    return {
        "source": str(source),
        "original": str(destination),
        "sha256": expected,
        "same_inode": True,
    }


def validate_marker(payload: bytes, owner: int, mode: int) -> dict[str, Any]:
    if owner != 0 or mode & 0o022 or len(payload) > 16384:
        raise RuntimeError("unsafe preparation guest marker")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError("invalid preparation guest marker record")
    if (
        value.get("preparation") is not True
        or type(value.get("slot")) is not int
        or value["slot"] not in (0, 1)
        or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("resource", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("image_sha256", "")))
    ):
        raise RuntimeError("not an explicitly marked disposable preparation guest")
    return value


def require_guest_root() -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("sealing helper is guest-root only")
    descriptor = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("regular preparation guest marker required")
        return validate_marker(
            stream.read(16385), info.st_uid, stat.S_IMODE(info.st_mode)
        )


def machine_id_identity(path: Path) -> dict[str, str | int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(
            "linked or nonregular machine-id refused before sealing mutations"
        )
    return {"device": info.st_dev, "inode": info.st_ino, "sha256": file_hash(path)}


def preparation_preflight() -> tuple[dict[str, Any], dict[str, str | int] | None]:
    # All validation is read-only and precedes evidence creation or original-data moves.
    marker = require_guest_root()
    return marker, machine_id_identity(MACHINE_ID)


def main() -> None:
    marker, machine_before = preparation_preflight()
    EVIDENCE.mkdir(mode=0o700, exist_ok=True)
    info = EVIDENCE.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("private root-owned preparation evidence directory required")
    generation = EVIDENCE / ("sealing-" + uuid.uuid4().hex)
    generation.mkdir(mode=0o700)
    sync_directory(EVIDENCE)
    private_capture(
        generation, "guest-marker.json", json.dumps(marker, sort_keys=True).encode()
    )
    debug = Path("/tmp/debugbuild")
    debug_receipt = None
    if debug.exists() or debug.is_symlink():
        debug_receipt = archive_debug_tree(debug, generation)
    cloud = archive_cloud_metadata(Path("/var/lib/cloud"), generation)
    identities = []
    for path in sorted(Path("/etc/ssh").glob("ssh_host_*")):
        identities.append(archive_identity_file(path, generation, path.name))
    authorized = Path("/home/runner/.ssh/authorized_keys")
    if authorized.exists() or authorized.is_symlink():
        identities.append(
            archive_identity_file(authorized, generation, "runner-authorized_keys")
        )
    if machine_id_identity(MACHINE_ID) != machine_before:
        raise RuntimeError(
            "machine-id identity changed after read-only preflight; do not clean"
        )
    if machine_before is not None:
        expected = machine_before["sha256"]
        captured = private_capture(
            generation, "machine-id.before", MACHINE_ID.read_bytes()
        )
        if (
            file_hash(captured) != expected
            or machine_id_identity(MACHINE_ID) != machine_before
        ):
            raise RuntimeError("machine-id capture changed; do not clean")
    receipt = {
        "scope": "disposable preparation guest only",
        "cloud": cloud,
        "identities": identities,
        "debugbuild": debug_receipt,
        "logs_deleted": False,
        "next": "cloud-init clean --machine-id on fresh metadata only",
    }
    completed = private_capture(
        generation, "receipt.json", json.dumps(receipt, sort_keys=True).encode()
    )
    print(
        json.dumps(
            {
                "status": "originals_retained_verified",
                "evidence": str(generation),
                "cloud_entries": cloud["entries"],
                "identity_files": len(identities),
                "receipt_sha256": file_hash(completed),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
