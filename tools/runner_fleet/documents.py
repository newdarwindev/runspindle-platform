"""Verify the separate, offline document-renderer closure. Never install anything."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import stat
import sys
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import TypedDict, cast

from .quality import digest, verified_file

PACKAGES = {"reportlab": "4.4.9", "pillow": "13.0.0", "charset-normalizer": "3.5.2"}
INTERPRETER = "/opt/runspindle-documents/bin/python"
PYTHON = "3.12.3"


class Wheel(TypedDict):
    name: str
    version: str
    filename: str
    sha256: str
    bytes: int
    notice_members: list[str]


class Closure(TypedDict):
    schema_version: int
    architecture: str
    implementation: str
    python: str
    interpreter: str
    requirements_sha256: str
    wheels: list[Wheel]


def load(source: Path) -> Closure:
    pins = json.loads((source / "fleet/image-inputs.json").read_text())["documents"]
    if pins["closure"] != "fleet/document-closure.json":
        raise ValueError("unexpected document closure path")
    path = source / pins["closure"]
    verified_file(path, pins["closure_sha256"])
    closure = cast(Closure, json.loads(path.read_text()))
    if (
        closure["schema_version"] != 1
        or closure["architecture"] != "linux/x86_64"
        or closure["implementation"] != "CPython"
        or closure["python"] != PYTHON
        or closure["interpreter"] != INTERPRETER
        or len(closure["wheels"]) != len(PACKAGES)
        or {row["name"]: row["version"] for row in closure["wheels"]} != PACKAGES
    ):
        raise ValueError("wrong document closure schema, platform or versions")
    expected = ""
    for row in closure["wheels"]:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", row["filename"])
            or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
            or type(row["bytes"]) is not int
            or row["bytes"] <= 0
            or not row["notice_members"]
        ):
            raise ValueError("invalid document wheel inventory")
        expected += f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}\n"
    requirements = source / "requirements-documents.txt"
    verified_file(requirements, closure["requirements_sha256"])
    if requirements.read_text() != expected:
        raise ValueError("document requirements differ from wheel pins")
    return closure


def wheel_notices(path: Path, row: Wheel) -> dict[str, str]:
    """Read only verified ZIP bytes, retaining every selected font/library notice."""
    import hashlib

    with zipfile.ZipFile(path) as archive:
        names: set[str] = set()
        for member in archive.infolist():
            member_path = PurePosixPath(member.filename)
            if (
                member_path.is_absolute()
                or ".." in member_path.parts
                or "\\" in member.filename
                or member.filename in names
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError("unsafe or duplicate document wheel member")
            names.add(member.filename)
        dist = row["name"].replace("-", "_") + "-" + row["version"] + ".dist-info/"
        metadata = BytesParser().parsebytes(archive.read(dist + "METADATA"))
        if (
            metadata["Name"].lower().replace("_", "-") != row["name"]
            or metadata["Version"] != row["version"]
        ):
            raise ValueError("document wheel metadata differs from pin")
        notices: dict[str, str] = {}
        for name in row["notice_members"]:
            data = archive.read(name)
            if not data.strip():
                raise ValueError("empty document dependency notice")
            notices[name] = hashlib.sha256(data).hexdigest()
        return notices


def verify(source: Path, downloads: Path, architecture: str) -> dict[str, object]:
    closure = load(source)
    if architecture != closure["architecture"]:
        raise ValueError("wrong document architecture")
    verified_file(
        downloads / "requirements-documents.txt", closure["requirements_sha256"]
    )
    directory = downloads / "document-wheels"
    names = {row["filename"] for row in closure["wheels"]}
    if directory.is_symlink() or {p.name for p in directory.iterdir()} != names:
        raise ValueError("missing or extra document wheel")
    notices = {}
    for row in closure["wheels"]:
        path = directory / row["filename"]
        verified_file(path, row["sha256"], row["bytes"])
        notices.update(wheel_notices(path, row))
    return {
        "status": "verified",
        "closure_sha256": digest(source / "fleet/document-closure.json"),
        "wheel_count": len(names),
        "notice_sha256": notices,
    }


def verify_installed_wheel(archive_path: Path, row: Wheel, prefix: Path) -> None:
    distribution = importlib.metadata.distribution(row["name"])
    if distribution.version != row["version"]:
        raise ValueError("installed document dependency version differs")
    # Compare immutable archive files, including fonts and notices. Pip rewrites
    # RECORD and creates entry-point scripts; no scripts exist in these wheels.
    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            if name.endswith(("/", ".dist-info/RECORD")):
                continue
            path = Path(str(distribution.locate_file(name)))
            if (
                not path.resolve().is_relative_to(prefix)
                or not path.is_file()
                or path.is_symlink()
                or path.read_bytes() != archive.read(name)
            ):
                raise ValueError("installed document wheel bytes differ")


def installed(source: Path, downloads: Path) -> dict[str, object]:
    """Record actual installed bytes after the separately authorized offline install."""
    closure = load(source)
    result = verify(source, downloads, "linux/" + platform.machine())
    if (
        platform.system() != "Linux"
        or platform.python_implementation() != "CPython"
        or platform.python_version() != PYTHON
        or sys.executable != INTERPRETER
        or sys.prefix != str(Path(INTERPRETER).parent.parent)
    ):
        raise ValueError("document interpreter is not the pinned isolated environment")
    for row in closure["wheels"]:
        verify_installed_wheel(
            downloads / "document-wheels" / row["filename"], row, Path(sys.prefix)
        )
    result.update(python=PYTHON, interpreter=INTERPRETER, packages=PACKAGES)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    result = (
        installed(args.source, args.downloads)
        if args.installed
        else verify(args.source, args.downloads, "linux/" + platform.machine())
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
