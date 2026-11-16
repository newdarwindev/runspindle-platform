"""Integrity and admission for local synthetic fixtures and optional upstream lanes."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

DEPENDENCIES = {
    "Temporal",
    "OpenBao",
    "PinchTab",
    "KasmVNC",
    "Collector",
    "Langfuse",
    "DSPy",
}
TARGETS = {"linux/amd64", "linux/arm64", "darwin/amd64", "darwin/arm64"}
SHA256 = re.compile(r"[a-f0-9]{64}")
TOOLCHAIN = {
    "implementation": "CPython",
    "version": "3.14.8",
    "target": "linux/amd64",
    "executable_sha256": "098f865cad2de188bdbad3baac99dd91ebb74bab361d2359f0baaf1929b1dd82",
    "dependencies": "stdlib-only",
    "qualification": "observed_local_synthetic_only",
}


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate manifest key")
        result[key] = value
    return result


def parse(data: bytes) -> Any:
    require(len(data) <= 1024 * 1024, "manifest size limit")
    return json.loads(data, object_pairs_hook=unique)


def safe_path(name: str) -> str:
    path = PurePosixPath(name)
    require(
        bool(name)
        and not path.is_absolute()
        and str(path) == name
        and all(part not in (".", "..") for part in path.parts)
        and name != "."
        and "\\" not in name
        and "\x00" not in name,
        "unsafe fixture path",
    )
    return name


def validate_inventory(files: dict[str, bytes], expected: dict[str, str]) -> None:
    require(bool(files) and set(files) == set(expected), "incomplete file inventory")
    for name, content in files.items():
        safe_path(name)
        require(SHA256.fullmatch(expected[name]) is not None, "invalid checksum")
        require(digest(content) == expected[name], "corrupted fixture bytes")
        require(
            not any(str(p) in files for p in PurePosixPath(name).parents),
            "file ancestor collision",
        )


def load_manifest(root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    require(
        not any(p.is_symlink() for p in [root / "manifest.json", root, *root.parents]),
        "symlink fixture",
    )
    manifest = parse((root / "manifest.json").read_bytes())
    require(
        type(manifest) is dict
        and manifest.get("schema") == "runspindle.synthetic-harness.v1",
        "manifest schema",
    )
    require(
        manifest.get("evidence_class") == "synthetic", "synthetic evidence required"
    )
    require(
        set(manifest["targets"]) == TARGETS
        and len(manifest["targets"]) == len(TARGETS),
        "target inventory",
    )
    require(
        manifest["toolchain"] == TOOLCHAIN,
        "unqualified toolchain",
    )
    require(set(manifest["dependencies"]) == DEPENDENCIES, "dependency inventory")
    require(
        {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        == set(manifest["files"]) | {"manifest.json"},
        "incomplete file inventory",
    )
    files: dict[str, bytes] = {}
    for name in manifest["files"]:
        safe_path(name)
        path = root / name
        require(
            not any(p.is_symlink() for p in [path, *path.parents]), "symlink fixture"
        )
        require(path.resolve().is_relative_to(root.resolve()), "escaped fixture")
        require(path.stat().st_size <= 65536, "fixture size limit")
        files[name] = path.read_bytes()
    validate_inventory(files, manifest["files"])
    for name, entry in manifest["dependencies"].items():
        require(
            entry
            == {
                "version": "synthetic-v1",
                "source_revision": digest(files["dependency.txt"]),
                "artifact": "dependency.txt",
                "sha256": digest(files["dependency.txt"]),
                "license": "LicenseRef-Original-Synthetic-Fixture",
                "notice": "NOTICE.txt",
                "qualification": "double_only",
                "upstream": name,
            },
            "floating or unqualified synthetic dependency",
        )
    require(
        manifest["sbom"]
        == {
            "format": "runspindle.synthetic-sbom.v1",
            "components": sorted(DEPENDENCIES),
            "notice_sha256": digest(files["NOTICE.txt"]),
        },
        "SBOM/notice mismatch",
    )
    return manifest, files


def toolchain_status() -> dict[str, str]:
    """Observed interpreter bytes are a narrow pin, not a full OS closure."""
    actual = digest(Path(sys.executable).read_bytes())
    matched = (
        platform.python_implementation() == TOOLCHAIN["implementation"]
        and platform.python_version() == TOOLCHAIN["version"]
        and platform.system() == "Linux"
        and platform.machine() == "x86_64"
        and actual == TOOLCHAIN["executable_sha256"]
    )
    return {
        "status": "passed" if matched else "blocked",
        "executable_sha256": actual,
        "reason": "observed_synthetic_interpreter_pin"
        if matched
        else "unqualified_interpreter_or_target",
    }


def integration_plan(name: str, *, opt_in: bool = False) -> dict[str, str]:
    """Describe existing separate lanes; never download, launch or infer qualification.

    Every entry stays blocked until the named lane independently validates its
    own pins, prepared bytes, isolation and host prerequisites at execution time.
    """
    require(name in DEPENDENCIES, "unknown upstream")
    selected = {
        "Temporal": ("temporal/dependencies.json", "tools/temporal_lane.py"),
        "Collector": (
            "telemetry/dependencies.json",
            "tools/collector_privacy_check.py",
        ),
        "Langfuse": (
            "telemetry/langfuse-qualification.json",
            "blocked_dependency_closure",
        ),
    }
    pins, lane = selected.get(name, ("unselected", "blocked_no_qualified_lane"))
    return {
        "upstream": name,
        "status": "blocked" if opt_in else "not_run",
        "reason": "independent_pins_preparation_and_isolation_required"
        if opt_in
        else "explicit_opt_in_required",
        "pins": pins,
        "lane": lane,
        "compatibility": "unknown",
    }
