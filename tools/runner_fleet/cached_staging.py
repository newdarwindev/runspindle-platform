"""Restore only missing pinned preparation inputs; preserve every staging gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .input_cache import InputCache
from .quality import verified_file
from .staging import verify as verify_staged


def inventory(source: Path) -> dict[str, str]:
    pins = json.loads((source / "fleet/image-inputs.json").read_text())
    closure_files = {
        "quality": (pins["quality"]["closure"], pins["quality"]["closure_sha256"]),
        "rust": (pins["rust_closure"]["manifest"], pins["rust_closure"]["sha256"]),
        "builder": (
            pins["runtime_builder"]["manifest"],
            pins["runtime_builder"]["sha256"],
        ),
    }
    records = {}
    for key, (path, digest) in closure_files.items():
        verified_file(source / path, digest)
        records[key] = json.loads((source / path).read_text())
    quality, rust, builder = records["quality"], records["rust"], records["builder"]
    files = {
        "ubuntu.img": pins["ubuntu"]["sha256"],
        "actions-runner-linux-x64-2.338.2.tar.gz": pins["runner"]["sha256"],
        "rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz": pins["rust"]["sha256"],
        "node.tar.xz": pins["node"]["sha256"],
        pins["temporal"]["filename"]: pins["temporal"]["sha256"],
        "LICENSE-CLI": pins["temporal"]["license_sha256"],
        "requirements-quality.txt": quality["requirements_sha256"],
        "quality-node/package.json": quality["node"]["package_json_sha256"],
        "quality-node/package-lock.json": quality["node"]["lock_sha256"],
        "quality-node-modules.tar.gz": quality["node"]["modules_archive_sha256"],
        "cargo-closure.tar.gz": rust["cargo_archive_sha256"],
        builder["archive"]: builder["archive_sha256"],
    }
    files.update(
        {
            f"{name}-source.tar.gz": rust[name]["source_archive_sha256"]
            for name in ("core", "host")
        }
    )
    files.update(
        {
            "quality-wheels/" + item["filename"]: item["sha256"]
            for item in quality["wheels"]
        }
    )
    for relative in files:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError("Unsafe pinned staging path")
    return files


def restore_missing(source: Path, downloads: Path, cache: InputCache) -> dict[str, int]:
    restored = missing = 0
    for relative, expected in inventory(source).items():
        destination = downloads / relative
        if destination.exists() or destination.is_symlink():
            # Never use a cache hit to cover up an altered existing staged file.
            verified_file(destination, expected)
        elif cache.has(expected):
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cache.materialize(expected, destination)
            restored += 1
        else:
            missing += 1
    return {"restored_inputs": restored, "missing_inputs": missing}


def populate_verified(
    source: Path, downloads: Path, cache: InputCache
) -> dict[str, int]:
    # Caller must run the full archive/closure verifier first. put() additionally
    # validates each exact byte digest and never replaces an existing CAS entry.
    reused = imported = 0
    for relative, expected in inventory(source).items():
        result = cache.put(downloads / relative, expected)
        reused += result == "existing"
        imported += result != "existing"
    return {"reused_inputs": reused, "imported_inputs": imported}


def prepare_inputs(
    source: Path, downloads: Path, cache: InputCache, acquire_ubuntu: Callable[[], None]
) -> dict[str, int]:
    """The image builder's pre-guest path; acquisition never replaces bad bytes."""
    restored = restore_missing(source, downloads, cache)
    ubuntu = downloads / "ubuntu.img"
    if not ubuntu.exists():
        acquire_ubuntu()
    verified_file(ubuntu, inventory(source)["ubuntu.img"])
    verify_staged(source, downloads)
    return restored | populate_verified(source, downloads, cache)
