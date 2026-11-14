"""Bind image preparation and fleet preflight to exact controller bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .quality import digest

FIXED = (
    "tools/fleet.py",
    "tools/temporal_lane.py",
    "requirements-quality.txt",
    "requirements-documents.txt",
    "temporal/dependencies.json",
    "fleet/config.json",
    "fleet/egress.json",
    "fleet/image-inputs.json",
    "fleet/quality-closure.json",
    "fleet/document-closure.json",
    "fleet/DOCUMENT_NOTICES.md",
    "fleet/rust-closure.json",
    "fleet/builder-closure.json",
    "fleet/builder-x64.Containerfile",
)
RECIPE = (
    "tools/runner_fleet/build_image.py",
    "tools/runner_fleet/prepare_image.sh",
    "tools/runner_fleet/prepare_temporal.sh",
)
CLOSURE = (
    "requirements-quality.txt",
    "requirements-documents.txt",
    "temporal/dependencies.json",
    "fleet/image-inputs.json",
    "fleet/quality-closure.json",
    "fleet/document-closure.json",
    "fleet/DOCUMENT_NOTICES.md",
    "fleet/rust-closure.json",
    "fleet/builder-closure.json",
    "fleet/builder-x64.Containerfile",
)


def record(files: dict[str, str], names: tuple[str, ...] | None = None) -> str:
    selected = files if names is None else {name: files[name] for name in names}
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def binding(root: Path) -> dict[str, str]:
    paths = list(FIXED)
    tools = root / "tools/runner_fleet"
    if not tools.is_dir():
        raise ValueError("controller source missing")
    paths.extend(
        str(path.relative_to(root))
        for path in sorted(tools.iterdir())
        if path.suffix in (".py", ".sh")
    )
    if not set(RECIPE) <= set(paths):
        raise ValueError("image recipe missing")
    files: dict[str, str] = {}
    for name in paths:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("controller file missing or unsafe")
        files[name] = digest(path)
    return {
        "controller_sha256": record(files),
        "recipe_sha256": record(files, RECIPE),
        "tool_closure_sha256": record(files, CLOSURE),
    }
