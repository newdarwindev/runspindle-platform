"""Actual image-builder staging path with synthetic pinned 22-input closure."""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet import cached_staging, input_cache
from tools.runner_fleet.staging import verify


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(root: Path) -> tuple[Path, Path]:
    source = root / "source"
    downloads = root / "seed"
    source.mkdir()
    downloads.mkdir()
    (source / "fleet").mkdir()

    def file(name: str, data: bytes = b"synthetic pinned bytes") -> str:
        p = downloads / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return sha(p)

    fixed = {
        name: file(name)
        for name in [
            "ubuntu.img",
            "actions-runner-linux-x64-2.338.2.tar.gz",
            "rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz",
            "node.tar.xz",
            "temporal.tar.gz",
            "LICENSE-CLI",
            "builder.tar.gz",
        ]
    }
    for name in [
        "core-source.tar.gz",
        "host-source.tar.gz",
        "cargo-closure.tar.gz",
        "quality-node-modules.tar.gz",
    ]:
        with tarfile.open(downloads / name, "w:gz"):
            pass
    requirements = file("requirements-quality.txt")
    (source / "requirements-quality.txt").write_bytes(
        (downloads / "requirements-quality.txt").read_bytes()
    )
    wheels = [
        {
            "filename": f"w{i}.whl",
            "sha256": file(f"quality-wheels/w{i}.whl"),
            "bytes": len(b"synthetic pinned bytes"),
        }
        for i in range(8)
    ]
    quality = {
        "schema_version": 1,
        "architecture": "linux/x86_64",
        "python": "3.12",
        "requirements_sha256": requirements,
        "wheels": wheels,
        "node": {
            "package_json_sha256": file("quality-node/package.json"),
            "lock_sha256": file("quality-node/package-lock.json"),
            "modules_archive_sha256": sha(downloads / "quality-node-modules.tar.gz"),
            "bytes": (downloads / "quality-node-modules.tar.gz").stat().st_size,
        },
    }
    rust: dict[str, Any] = {
        "schema_version": 1,
        "architecture": "linux/x86_64",
        "cargo_archive_sha256": sha(downloads / "cargo-closure.tar.gz"),
        "cargo_archive_bytes": (downloads / "cargo-closure.tar.gz").stat().st_size,
        "crate_archives": {},
    }
    for name in ["core", "host"]:
        rust[name] = {
            "commit": name + "-synthetic",
            "source_archive_sha256": sha(downloads / (name + "-source.tar.gz")),
            "source_archive_bytes": (downloads / (name + "-source.tar.gz"))
            .stat()
            .st_size,
        }
    (source / "recipe").write_text("synthetic recipe")
    builder = {
        "schema_version": 1,
        "architecture": "linux/x86_64",
        "rust_version": "1.94.1",
        "recipe": "recipe",
        "recipe_sha256": sha(source / "recipe"),
        "archive": "builder.tar.gz",
        "archive_sha256": fixed["builder.tar.gz"],
        "archive_bytes": (downloads / "builder.tar.gz").stat().st_size,
        "docker_loaded_builder_image_id": "sha256:synthetic",
    }
    for name, value in [("quality", quality), ("rust", rust), ("builder", builder)]:
        (source / f"fleet/{name}.json").write_text(json.dumps(value))
    pins = {
        "ubuntu": {"sha256": fixed["ubuntu.img"]},
        "runner": {"sha256": fixed["actions-runner-linux-x64-2.338.2.tar.gz"]},
        "rust": {
            "version": "1.94.1",
            "sha256": fixed["rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz"],
        },
        "node": {"sha256": fixed["node.tar.xz"]},
        "temporal": {
            "filename": "temporal.tar.gz",
            "sha256": fixed["temporal.tar.gz"],
            "license_sha256": fixed["LICENSE-CLI"],
        },
        "quality": {
            "closure": "fleet/quality.json",
            "closure_sha256": sha(source / "fleet/quality.json"),
        },
        "rust_closure": {
            "manifest": "fleet/rust.json",
            "sha256": sha(source / "fleet/rust.json"),
        },
        "runtime_builder": {
            "manifest": "fleet/builder.json",
            "sha256": sha(source / "fleet/builder.json"),
        },
        "offline_rust_dependencies": {
            name: rust[name]["commit"] for name in ["core", "host"]
        },
    }
    (source / "fleet/image-inputs.json").write_text(json.dumps(pins))
    return source, downloads


class StagingTests(unittest.TestCase):
    def test_fresh_staging_reuses_all_inputs_and_still_validates(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(input_cache, "MIN_FREE_BYTES", 0),
        ):
            root = Path(tmp)
            source, seed = fixture(root)
            cache = input_cache.InputCache(root / "shared-cache")
            acquire = Mock(side_effect=AssertionError("forbidden acquisition"))
            cold = cached_staging.prepare_inputs(source, seed, cache, acquire)
            self.assertGreater(cold["imported_inputs"], 0)
            fresh = root / "fresh-preparation"
            fresh.mkdir(mode=0o700)
            with patch.object(
                cached_staging, "verify_staged", wraps=verify
            ) as verifier:
                warm = cached_staging.prepare_inputs(source, fresh, cache, acquire)
            self.assertEqual(warm["restored_inputs"], 22)
            self.assertEqual(warm["missing_inputs"], 0)
            verifier.assert_called_once_with(source, fresh)
            acquire.assert_not_called()
            victim = fresh / "quality-node-modules.tar.gz"
            victim.chmod(0o600)
            victim.write_bytes(b"corrupt staged archive")
            with self.assertRaises(ValueError):
                cached_staging.prepare_inputs(source, fresh, cache, acquire)
            acquire.assert_not_called()
