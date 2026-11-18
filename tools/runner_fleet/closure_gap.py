"""Read-only Cargo archive coverage audit; never builds, fetches or qualifies CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import tomllib

REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"
METADATA_LIMIT = 4 * 1024**2
ARCHIVE_LIMIT = 16 * 1024**2
TOTAL_LIMIT = 512 * 1024**2


def read_regular(path: Path, limit: int) -> bytes:
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise ValueError("canonical input path required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("input type or size rejected")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > limit or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("input changed during bounded read")
        return raw


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def audit(lock: Path, manifest: Path, cache: Path) -> dict[str, Any]:
    lock_raw = read_regular(lock, METADATA_LIMIT)
    manifest_raw = read_regular(manifest, METADATA_LIMIT)
    packages = tomllib.loads(lock_raw.decode())["package"]
    pins = json.loads(manifest_raw, object_pairs_hook=unique)["crate_archives"]
    if type(packages) is not list or len(packages) > 5000 or type(pins) is not dict:
        raise ValueError("invalid closure inventory")
    if (
        not cache.is_absolute()
        or cache.resolve(strict=True) != cache
        or not cache.is_dir()
    ):
        raise ValueError("canonical Cargo cache directory required")
    registries: list[Path] = []
    with os.scandir(cache) as entries:
        for entry in entries:
            if len(registries) >= 16 or not entry.is_dir(follow_symlinks=False):
                raise ValueError("unexpected Cargo cache layout")
            registries.append(Path(entry.path))
    missing = []
    unsupported = []
    seen: set[str] = set()
    covered = local_packages = total_read = 0
    for package in packages:
        if type(package) is not dict:
            raise ValueError("invalid package entry")
        name, version = package.get("name"), package.get("version")
        if (
            type(name) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) is None
            or type(version) is not str
            or re.fullmatch(r"[A-Za-z0-9.+-]{1,128}", version) is None
        ):
            raise ValueError("invalid bounded Cargo identity")
        archive = f"{name}-{version}.crate"
        source = package.get("source")
        if source is None:
            local_packages += 1
            continue
        if source != REGISTRY:
            unsupported.append({"name": name, "version": version})
            continue
        checksum = package.get("checksum")
        if (
            type(checksum) is not str
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            or archive in seen
        ):
            raise ValueError("invalid or duplicate registry identity")
        seen.add(archive)
        if pins.get(archive) == checksum:
            covered += 1
            continue
        verified_sizes = []
        for registry in registries:
            file = registry / archive
            if not file.exists() and not file.is_symlink():
                continue
            remaining = min(ARCHIVE_LIMIT, TOTAL_LIMIT - total_read)
            if remaining <= 0:
                raise ValueError("aggregate archive read budget exhausted")
            raw = read_regular(file, remaining)
            total_read += len(raw)
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError("local archive checksum mismatch")
            verified_sizes.append(len(raw))
        missing.append(
            {
                "archive": archive,
                "sha256": checksum,
                "locally_verified": bool(verified_sizes),
                "bytes": verified_sizes[0] if verified_sizes else None,
            }
        )
    return {
        "schema": "runspindle.cargo-closure-gap.v1",
        "lock_sha256": hashlib.sha256(lock_raw).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "registry_packages": len(seen),
        "manifest_covered": covered,
        "local_packages_not_audited": local_packages,
        "unsupported_source_packages": unsupported,
        "missing_from_manifest": missing,
        "locally_verified_missing_bytes": sum(row["bytes"] or 0 for row in missing),
        "registry_coverage": "complete"
        if not missing and not unsupported
        else "incomplete",
        "archive_index_and_source_closure": "not_qualified",
        "image_or_ci_qualification": False,
        "effects": "read_only_no_fetch_build_or_registration",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(args.lock, args.manifest, args.cache)
    except (ValueError, OSError, KeyError, TypeError, RecursionError):
        print(json.dumps({"status": "rejected", "reason": "invalid_or_changed_inputs"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
