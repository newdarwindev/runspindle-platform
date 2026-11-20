#!/usr/bin/env python3
"""Verify pinned gzip OCI layers and read package metadata without extraction."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Protocol, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.langfuse_image_inventory import inventory  # noqa: E402

GZIP_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
DPKG = "var/lib/dpkg/status"
APK = "lib/apk/db/installed"
DB_PATHS = {DPKG, APK}
READ_SIZE = 1024 * 1024
MAX_DIFF_BYTES = 4 * 1024**3
MAX_MEMBERS = 500_000
MAX_SELECTED_BYTES = 128 * 1024**2
PACKAGE = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@./_+\-]{0,213}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:+_~\-]{0,127}")


class Readable(Protocol):
    def read(self, size: int, /) -> bytes: ...


class DigestReader:
    def __init__(self, stream: Readable, limit: int) -> None:
        self.stream, self.limit = stream, limit
        self.size = 0
        self.hash = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise ValueError("unbounded layer read")
        chunk = self.stream.read(min(size, self.limit - self.size + 1))
        self.size += len(chunk)
        if self.size > self.limit:
            raise ValueError("layer size limit exceeded")
        self.hash.update(chunk)
        return chunk

    def digest(self) -> str:
        return "sha256:" + self.hash.hexdigest()


def path_of(name: str) -> str:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or len(name) > 4096
        or len(path.parts) > 128
        or ".." in path.parts
    ):
        raise ValueError("invalid OCI member path")
    return path.as_posix()


def selected(path: str) -> bool:
    if path in DB_PATHS:
        return True
    parts = path.split("/")
    return parts[-1] == "package.json" and (
        (
            len(parts) >= 3
            and parts[-3] == "node_modules"
            and not parts[-2].startswith(("@", "."))
        )
        or (
            len(parts) >= 4
            and parts[-4] == "node_modules"
            and parts[-3].startswith("@")
        )
    )


def read_member(archive: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> bytes:
    if not member.isfile() or not 0 < member.size <= limit:
        raise ValueError("unsupported package metadata member")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("missing package metadata member")
    with stream:
        value = stream.read(limit + 1)
    if len(value) != member.size:
        raise ValueError("truncated package metadata member")
    return value


def overlay(
    archive: tarfile.TarFile,
    retained: dict[str, bytes],
    missing_links: dict[str, str] | None = None,
) -> dict[str, bytes]:
    removed: set[str] = set()
    opaque: set[PurePosixPath] = set()
    added: dict[str, bytes] = {}
    added_bytes = 0
    seen: set[str] = set()
    for member in archive:
        path = path_of(member.name)
        if len(seen) >= MAX_MEMBERS or path in seen:
            raise ValueError("OCI member count or duplicate path")
        seen.add(path)
        full = PurePosixPath(path)
        if full.name.startswith(".wh."):
            if not member.isfile() or member.size != 0 or full.name == ".wh.":
                raise ValueError("invalid OCI whiteout")
            if full.name == ".wh..wh..opq":
                opaque.add(full.parent)
            else:
                removed.add(full.with_name(full.name[4:]).as_posix())
            continue
        if not member.isdir():
            removed.add(path)
        if not selected(path):
            continue
        limit = (16 if path in DB_PATHS else 1) * 1024**2
        if member.islnk():
            destination = path_of(member.linkname)
            if (
                missing_links is None
                or destination not in seen
                or destination == path
                or member.size
            ):
                raise ValueError("unresolved OCI package hardlink")
            missing_links[path] = destination
            continue
        value = read_member(archive, member, limit)
        added_bytes += len(value)
        if added_bytes > MAX_SELECTED_BYTES:
            raise ValueError("package metadata size limit exceeded")
        added[path] = value
    for path in retained:
        parents = PurePosixPath(path).parents
        if path not in removed and not any(
            p in opaque or p.as_posix() in removed for p in parents
        ):
            added[path] = retained[path]
            added_bytes += len(retained[path])
    if added_bytes > MAX_SELECTED_BYTES:
        raise ValueError("package metadata size limit exceeded")
    return added


def scan_layer(
    raw: BinaryIO,
    descriptor: dict[str, Any],
    scan: Callable[[tarfile.TarFile], dict[str, bytes]],
) -> tuple[dict[str, bytes], int]:
    raw.seek(0)
    compressed = DigestReader(raw, descriptor["bytes"])
    with gzip.GzipFile(fileobj=cast(BinaryIO, compressed)) as expanded:
        diff = DigestReader(expanded, MAX_DIFF_BYTES)
        with tarfile.open(
            fileobj=cast(BinaryIO, diff), mode="r|", bufsize=128 * 1024
        ) as archive:
            result = scan(archive)
        while diff.read(READ_SIZE):
            pass
    if (
        compressed.size != descriptor["bytes"]
        or compressed.digest() != descriptor["digest"]
        or diff.digest() != descriptor["uncompressed_digest"]
    ):
        raise ValueError("compressed layer or uncompressed layer digest mismatch")
    return result, diff.size


def layer(
    cache: Path, descriptor: dict[str, Any], retained: dict[str, bytes]
) -> tuple[dict[str, bytes], int]:
    if descriptor["media_type"] != GZIP_LAYER:
        raise ValueError("expected pinned gzip layer")
    expected = descriptor["digest"]
    path = cache / expected.split(":", 1)[1]
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as raw:
        info = os.fstat(raw.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != descriptor["bytes"]:
            raise ValueError("layer type or compressed size mismatch")
        preflight = DigestReader(raw, descriptor["bytes"])
        while preflight.read(READ_SIZE):
            pass
        if preflight.size != descriptor["bytes"] or preflight.digest() != expected:
            raise ValueError("compressed layer digest mismatch")
        links: dict[str, str] = {}
        next_retained, size = scan_layer(
            raw, descriptor, lambda archive: overlay(archive, retained, links)
        )
        if links:
            targets = set(links.values())

            def extract_targets(archive: tarfile.TarFile) -> dict[str, bytes]:
                found: dict[str, bytes] = {}
                total = 0
                for member in archive:
                    path = path_of(member.name)
                    if path in targets:
                        value = read_member(archive, member, READ_SIZE)
                        total += len(value)
                        if total > MAX_SELECTED_BYTES:
                            raise ValueError("package hardlink target capacity")
                        found[path] = value
                return found

            contents, _ = scan_layer(raw, descriptor, extract_targets)
            if contents.keys() != targets:
                raise ValueError("missing package hardlink target")
            next_retained.update(
                {path: contents[target] for path, target in links.items()}
            )
            if sum(map(len, next_retained.values())) > MAX_SELECTED_BYTES:
                raise ValueError("package metadata size limit exceeded")
    return next_retained, size


def package_pair(name: str, version: str) -> tuple[str, str]:
    if PACKAGE.fullmatch(name) is None or VERSION.fullmatch(version) is None:
        raise ValueError("invalid package name or version metadata")
    return name, version


def os_packages(retained: dict[str, bytes]) -> dict[str, Any]:
    databases = DB_PATHS & retained.keys()
    if len(databases) != 1:
        raise ValueError("expected one installed OS package database")
    path = next(iter(databases))
    dpkg = path == DPKG
    rows = set()
    for stanza in retained[path].decode().split("\n\n"):
        fields = dict(
            line.split(":", 1)
            for line in stanza.splitlines()
            if line and not line[0].isspace() and ":" in line
        )
        if not fields or (
            dpkg and fields.get("Status", "").split()[-2:] != ["ok", "installed"]
        ):
            continue
        name_key, version_key, arch_key = (
            ("Package", "Version", "Architecture") if dpkg else ("P", "V", "A")
        )
        name, version = package_pair(
            fields.get(name_key, "").strip(), fields.get(version_key, "").strip()
        )
        arch = fields.get(arch_key, "").strip()
        if re.fullmatch(r"[a-z0-9_.-]{1,32}", arch) is None:
            raise ValueError("invalid OS package architecture")
        rows.add((name, version, arch))
    if not rows:
        raise ValueError("empty installed OS package database")
    return {
        "manager": "dpkg" if dpkg else "apk",
        "database_sha256": hashlib.sha256(retained[path]).hexdigest(),
        "packages": [
            dict(name=n, version=v, architecture=a) for n, v, a in sorted(rows)
        ],
    }


def node_packages(retained: dict[str, bytes]) -> dict[str, Any]:
    packages: Counter[tuple[str, str]] = Counter()
    unversioned = 0
    for path, raw in retained.items():
        if path in DB_PATHS:
            continue
        row = json.loads(raw)
        if not isinstance(row, dict):
            raise ValueError("invalid Node package manifest")
        name, version = row.get("name"), row.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            unversioned += 1
            continue
        packages[package_pair(name, version)] += 1
    return {
        "packages": [
            dict(name=n, version=v, manifests=count)
            for (n, v), count in sorted(packages.items())
        ],
        "manifests_without_name_or_version": unversioned,
    }


def audit(metadata: Path, cache: Path) -> dict[str, Any]:
    pinned = inventory(metadata)
    root = cache.resolve(strict=True)
    result = {}
    for role, image in pinned["images"].items():
        retained: dict[str, bytes] = {}
        sizes = []
        for descriptor in image["layers"]:
            retained, size = layer(root, descriptor, retained)
            sizes.append(size)
        result[role] = {
            "reference": image["reference"],
            "layer_count": len(sizes),
            "uncompressed_layer_bytes": sizes,
            "installed_os_packages": os_packages(retained),
            "physical_node_package_manifests": node_packages(retained),
        }
    return {
        "schema_version": 1,
        "evidence_class": "read_only_image_layer_inventory",
        "target": pinned["target"],
        "status": "layer_bytes_and_visible_package_metadata_verified",
        "langfuse_source_revision": pinned["langfuse_source_revision"],
        "images": result,
        "minio": "source_fallback",
        "service_execution": "not_run",
        "langfuse_readback": "not_run",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--layers", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = audit(args.metadata, args.layers)
    except (OSError, ValueError, EOFError, RecursionError, tarfile.TarError):
        print("invalid or incomplete Langfuse dependency layers", file=sys.stderr)
        return 78
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
