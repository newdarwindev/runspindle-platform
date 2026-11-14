"""Verify the offline quality-tool closure before it enters a preparation guest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def verified_file(path: Path, expected: str, size: int | None = None) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or not re.fullmatch(r"[0-9a-f]{64}", expected)
        or (size is not None and path.stat().st_size != size)
        or digest(path) != expected
    ):
        raise ValueError("staged quality archive missing or changed")


def verify(source: Path, downloads: Path, architecture: str) -> dict[str, object]:
    pins = json.loads((source / "fleet/image-inputs.json").read_text())
    closure_path = source / pins["quality"]["closure"]
    verified_file(closure_path, pins["quality"]["closure_sha256"])
    closure = json.loads(closure_path.read_text())
    if (
        closure.get("schema_version") != 1
        or closure.get("architecture") != "linux/x86_64"
        or architecture != "linux/x86_64"
        or closure.get("python") != "3.12"
    ):
        raise ValueError("wrong quality closure architecture or schema")
    verified_file(source / "requirements-quality.txt", closure["requirements_sha256"])
    verified_file(
        downloads / "requirements-quality.txt", closure["requirements_sha256"]
    )
    names = {item["filename"] for item in closure["wheels"]}
    if len(names) != 8 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", name) for name in names
    ):
        raise ValueError("invalid quality wheel inventory")
    actual = {path.name for path in (downloads / "quality-wheels").iterdir()}
    if actual != names:
        raise ValueError("missing or extra quality wheel")
    for item in closure["wheels"]:
        verified_file(
            downloads / "quality-wheels" / item["filename"],
            item["sha256"],
            item["bytes"],
        )
    node = closure["node"]
    verified_file(downloads / "quality-node/package.json", node["package_json_sha256"])
    verified_file(downloads / "quality-node/package-lock.json", node["lock_sha256"])
    verified_file(
        downloads / "quality-node-modules.tar.gz",
        node["modules_archive_sha256"],
        node["bytes"],
    )
    return {
        "status": "verified",
        "closure_sha256": pins["quality"]["closure_sha256"],
        "wheel_count": len(names),
        "node_archive_sha256": node["modules_archive_sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--architecture", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.source, args.downloads, args.architecture)))


if __name__ == "__main__":
    main()
