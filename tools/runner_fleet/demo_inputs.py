"""Audit external demo input bytes without executing, extracting or enrolling them."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .demo_notices import audit_notice
from .quality import verified_file


def _name(name: str, seen: set[str]) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or name in seen:
        raise ValueError("unsafe or duplicate archive member")
    seen.add(name)


def audit(manifest: dict[str, Any], downloads: Path) -> dict[str, object]:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("architecture") != "linux/x86_64"
    ):
        raise ValueError("wrong demo input schema or architecture")
    expected = {
        "playwright-1.56.1.tgz",
        "playwright-core-1.56.1.tgz",
        "1194-chromium-linux.zip",
        "1194-chromium-headless-shell-linux.zip",
        "1011-ffmpeg-linux.zip",
    }
    if {row["filename"] for row in manifest["archives"]} != expected or len(
        manifest["archives"]
    ) != 5:
        raise ValueError("wrong demo input inventory")
    files = 0
    notice_receipts: list[dict[str, object]] = []
    for row in manifest["archives"]:
        name = row["filename"]
        path = downloads / name
        verified_file(path, row["sha256"], row["bytes"])
        seen: set[str] = set()
        if name.endswith(".tgz"):
            if set(row["license_sha256"]) != {
                "package/LICENSE",
                "package/NOTICE",
                "package/ThirdPartyNotices.txt",
            }:
                raise ValueError("required NPM notices missing")
            integrity = (
                "sha512-"
                + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
            )
            if integrity != row["integrity"]:
                raise ValueError("NPM lock integrity changed")
            with tarfile.open(path, "r:gz") as archive:
                members: dict[str, bytes] = {}
                for member in archive:
                    _name(member.name, seen)
                    if not (member.isfile() or member.isdir()):
                        raise ValueError("unsupported NPM archive member")
                    if member.isfile():
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ValueError("missing NPM member bytes")
                        members[member.name] = stream.read()
                        files += 1
                package = json.loads(members["package/package.json"])
                if package["name"] + "-" + package["version"] + ".tgz" != name:
                    raise ValueError("NPM package identity changed")
                for notice, sha256 in row["license_sha256"].items():
                    if hashlib.sha256(members[notice]).hexdigest() != sha256:
                        raise ValueError("NPM notice changed")
                if name.startswith("playwright-core-"):
                    browsers = json.loads(members["package/browsers.json"])["browsers"]
                    revisions = {entry["name"]: entry["revision"] for entry in browsers}
                    if any(
                        revisions[key] != value
                        for key, value in {
                            "chromium": "1194",
                            "chromium-headless-shell": "1194",
                            "ffmpeg": "1011",
                        }.items()
                    ):
                        raise ValueError("Playwright browser revisions changed")
        else:
            required_notices = (
                {"COPYING.LGPLv2.1"} if name == "1011-ffmpeg-linux.zip" else set()
            )
            if set(row.get("license_sha256", {})) != required_notices:
                raise ValueError("browser archive notice inventory changed")
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise ValueError("corrupt ZIP member")
                for zip_member in archive.infolist():
                    _name(zip_member.filename, seen)
                    if stat.S_ISLNK(zip_member.external_attr >> 16):
                        raise ValueError("unsupported browser archive symlink")
                    files += int(not zip_member.is_dir())
                if name.startswith("1194-"):
                    pin = row["embedded_notice"]
                    notice_receipts.append(
                        audit_notice(archive.read(pin["member"]), pin)
                    )
                for notice, sha256 in row.get("license_sha256", {}).items():
                    if hashlib.sha256(archive.read(notice)).hexdigest() != sha256:
                        raise ValueError("browser notice changed")
    return {
        "status": "partial_external_inputs_audited",
        "archive_count": 5,
        "member_files": files,
        "embedded_notice_receipts": notice_receipts,
        "assertions": {
            "archive_digests_and_sizes": True,
            "npm_sha512_integrity": True,
            "npm_identity_and_notices": True,
            "playwright_browser_revisions": True,
            "safe_archive_paths": True,
            "matching_embedded_chromium_notice_bytes": len(notice_receipts) == 2,
            "chromium_notice_closure": False,
            "noble_ffmpeg_gh_dependency_closure": False,
            "guest_qualification": False,
        },
        "qualification": "blocked",
        "activation_allowed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    manifest = json.loads((source / "fleet/demo-browser-inputs.json").read_text())
    print(json.dumps(audit(manifest, args.downloads), sort_keys=True))


if __name__ == "__main__":
    main()
