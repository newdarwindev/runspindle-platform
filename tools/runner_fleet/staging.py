"""Verify immutable host-staged archives before transfer to an isolated guest."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path, PurePosixPath

from .quality import digest, verified_file
from .quality import verify as verify_quality


def safe_archive(path: Path, *, crate_hashes: dict[str, str] | None = None) -> None:
    seen_crates: dict[str, str] = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("unsafe staged archive member")
            if crate_hashes is None or not member.name.endswith(".crate"):
                continue
            filename = name.name
            if filename in seen_crates or filename not in crate_hashes:
                raise ValueError("unexpected Cargo crate archive")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("missing Cargo crate bytes")
            value = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
            seen_crates[filename] = value.hexdigest()
    if crate_hashes is not None and seen_crates != crate_hashes:
        raise ValueError("Cargo crate inventory changed")


def verify(source: Path, downloads: Path) -> dict[str, object]:
    pins = json.loads((source / "fleet/image-inputs.json").read_text())
    quality = verify_quality(source, downloads, "linux/x86_64")
    closure_path = source / pins["rust_closure"]["manifest"]
    verified_file(closure_path, pins["rust_closure"]["sha256"])
    closure = json.loads(closure_path.read_text())
    if (
        closure.get("schema_version") != 1
        or closure.get("architecture") != "linux/x86_64"
    ):
        raise ValueError("wrong Rust closure architecture or schema")
    for name in ("core", "host"):
        row = closure[name]
        if row["commit"] != pins["offline_rust_dependencies"][name]:
            raise ValueError("Rust source pin differs from closure")
        archive = downloads / f"{name}-source.tar.gz"
        verified_file(
            archive, row["source_archive_sha256"], row["source_archive_bytes"]
        )
        safe_archive(archive)
    cargo = downloads / "cargo-closure.tar.gz"
    verified_file(
        cargo, closure["cargo_archive_sha256"], closure["cargo_archive_bytes"]
    )
    safe_archive(cargo, crate_hashes=closure["crate_archives"])
    node = downloads / "quality-node-modules.tar.gz"
    safe_archive(node)
    builder_path = source / pins["runtime_builder"]["manifest"]
    verified_file(builder_path, pins["runtime_builder"]["sha256"])
    builder = json.loads(builder_path.read_text())
    if (
        builder.get("schema_version") != 1
        or builder.get("architecture") != "linux/x86_64"
        or builder.get("rust_version") != pins["rust"]["version"]
    ):
        raise ValueError("wrong runtime builder architecture or version")
    verified_file(source / builder["recipe"], builder["recipe_sha256"])
    verified_file(
        downloads / builder["archive"],
        builder["archive_sha256"],
        builder["archive_bytes"],
    )
    fixed = (
        ("actions-runner-linux-x64-2.338.2.tar.gz", pins["runner"]["sha256"]),
        ("rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz", pins["rust"]["sha256"]),
        ("node.tar.xz", pins["node"]["sha256"]),
        (pins["temporal"]["filename"], pins["temporal"]["sha256"]),
        ("LICENSE-CLI", pins["temporal"]["license_sha256"]),
    )
    for filename, expected in fixed:
        verified_file(downloads / filename, expected)
    return {
        "status": "verified",
        "rust_closure_sha256": digest(closure_path),
        "quality_closure_sha256": quality["closure_sha256"],
        "crate_count": len(closure["crate_archives"]),
        "builder_image_id": builder["docker_loaded_builder_image_id"],
    }
