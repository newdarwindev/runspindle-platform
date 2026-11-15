"""Synthetic contract fixtures, distinct from genuine upstream byte audits."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.runner_fleet.demo_inputs import audit


class DemoInputTests(unittest.TestCase):
    def fixtures(self, root: Path, revision: str = "1194") -> dict[str, Any]:
        rows = []
        for name in ("playwright", "playwright-core"):
            path = root / (name + "-1.56.1.tgz")
            notices = {
                "package/" + key: b"synthetic notice"
                for key in ("LICENSE", "NOTICE", "ThirdPartyNotices.txt")
            }
            members = {
                **notices,
                "package/package.json": json.dumps(
                    {"name": name, "version": "1.56.1"}
                ).encode(),
            }
            if name == "playwright-core":
                members["package/browsers.json"] = json.dumps(
                    {
                        "browsers": [
                            {"name": "chromium", "revision": revision},
                            {"name": "chromium-headless-shell", "revision": "1194"},
                            {"name": "ffmpeg", "revision": "1011"},
                        ]
                    }
                ).encode()
            with tarfile.open(path, "w:gz") as archive:
                for key, value in members.items():
                    info = tarfile.TarInfo(key)
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))
            rows.append(
                {
                    "filename": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                    "integrity": "sha512-"
                    + base64.b64encode(
                        hashlib.sha512(path.read_bytes()).digest()
                    ).decode(),
                    "license_sha256": {
                        key: hashlib.sha256(value).hexdigest()
                        for key, value in notices.items()
                    },
                }
            )
        for filename in (
            "1194-chromium-linux.zip",
            "1194-chromium-headless-shell-linux.zip",
            "1011-ffmpeg-linux.zip",
        ):
            path = root / filename
            notices = (
                {"COPYING.LGPLv2.1": b"synthetic license"}
                if filename.startswith("1011")
                else {}
            )
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("synthetic/binary", b"not executable")
                for key, value in notices.items():
                    archive.writestr(key, value)
            rows.append(
                {
                    "filename": filename,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                    "license_sha256": {
                        key: hashlib.sha256(value).hexdigest()
                        for key, value in notices.items()
                    },
                }
            )
        return {"schema_version": 1, "architecture": "linux/x86_64", "archives": rows}

    def test_partial_audit_never_qualifies_or_authorizes_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = audit(self.fixtures(root), root)
            self.assertEqual(result["qualification"], "blocked")
            self.assertFalse(result["activation_allowed"])
            assertions = result["assertions"]
            assert isinstance(assertions, dict)
            self.assertFalse(assertions["guest_qualification"])

    def test_wrong_architecture_inventory_integrity_and_notices_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixtures(root)
            changes = []
            mutations: tuple[Callable[[dict[str, Any]], object], ...] = (
                lambda m: m.update(architecture="linux/arm64"),
                lambda m: m["archives"].pop(),
                lambda m: m["archives"][0].update(integrity="sha512-wrong"),
                lambda m: m["archives"][0].update(license_sha256={}),
                lambda m: m["archives"][-1].update(license_sha256={}),
            )
            for mutate in mutations:
                changed = copy.deepcopy(manifest)
                mutate(changed)
                changes.append(changed)
            for changed in changes:
                with self.assertRaises(ValueError):
                    audit(changed, root)

    def test_changed_bytes_and_symlink_input_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixtures(root)
            path = root / manifest["archives"][0]["filename"]
            original = path.read_bytes()
            path.write_bytes(original + b"changed")
            with self.assertRaises(ValueError):
                audit(manifest, root)
            path.unlink()
            other = root / "other"
            other.write_bytes(original)
            path.symlink_to(other)
            with self.assertRaises(ValueError):
                audit(manifest, root)

    def test_wrong_embedded_browser_revision_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixtures(root, revision="1195")
            with self.assertRaises(ValueError):
                audit(manifest, root)

    def test_archive_path_escape_rejected_even_if_digest_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.fixtures(root)
            row = manifest["archives"][2]
            path = root / row["filename"]
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("../escape", b"synthetic")
            row.update(
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                bytes=path.stat().st_size,
            )
            with self.assertRaises(ValueError):
                audit(manifest, root)
