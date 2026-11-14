"""Synthetic offline document-closure tests. No downloads, installs or guests."""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.documents import Closure, load, verify, verify_installed_wheel
from runner_fleet.identity import FIXED, RECIPE, binding

ROOT = Path(__file__).resolve().parents[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DocumentClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source"
        self.downloads = root / "downloads"
        (self.source / "fleet").mkdir(parents=True)
        (self.downloads / "document-wheels").mkdir(parents=True)
        self.closure = cast(
            Closure, json.loads((ROOT / "fleet/document-closure.json").read_text())
        )
        for index in range(3):
            self.wheel(index)
        self.pin()

    def wheel(
        self,
        index: int,
        *,
        extra: str = "",
        missing_notice: bool = False,
        version: str | None = None,
    ) -> None:
        row = self.closure["wheels"][index]
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            dist = row["name"].replace("-", "_") + "-" + row["version"] + ".dist-info/"
            archive.writestr(
                dist + "METADATA",
                f"Name: {row['name']}\nVersion: {version or row['version']}\n",
            )
            for name in row["notice_members"]:
                if not missing_notice:
                    archive.writestr(name, "synthetic notice")
            if extra:
                archive.writestr(extra, "synthetic")
        value = data.getvalue()
        (self.downloads / "document-wheels" / row["filename"]).write_bytes(value)
        row["sha256"] = sha(value)
        row["bytes"] = len(value)

    def pin(self) -> None:
        requirements = "".join(
            f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}\n"
            for row in self.closure["wheels"]
        ).encode()
        self.closure["requirements_sha256"] = sha(requirements)
        for root in (self.source, self.downloads):
            (root / "requirements-documents.txt").write_bytes(requirements)
        data = (json.dumps(self.closure) + "\n").encode()
        (self.source / "fleet/document-closure.json").write_bytes(data)
        (self.source / "fleet/image-inputs.json").write_text(
            json.dumps(
                {
                    "documents": {
                        "closure": "fleet/document-closure.json",
                        "closure_sha256": sha(data),
                    }
                }
            )
        )

    def run_verify(self) -> dict[str, object]:
        return verify(self.source, self.downloads, "linux/x86_64")

    def test_exact_synthetic_closure_and_notices(self) -> None:
        result = self.run_verify()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["wheel_count"], 3)
        self.assertEqual(len(cast(dict[str, str], result["notice_sha256"])), 6)

    def test_missing_extra_changed_wheel(self) -> None:
        path = (
            self.downloads / "document-wheels" / self.closure["wheels"][0]["filename"]
        )
        data = path.read_bytes()
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.run_verify()
        path.unlink()
        with self.assertRaises(ValueError):
            self.run_verify()
        path.write_bytes(data)
        (path.parent / "extra.whl").write_bytes(b"extra")
        with self.assertRaises(ValueError):
            self.run_verify()

    def test_symlink_wheel_and_directory_rejected(self) -> None:
        path = (
            self.downloads / "document-wheels" / self.closure["wheels"][0]["filename"]
        )
        real = path.with_suffix(".saved")
        path.rename(real)
        real.rename(self.downloads / real.name)
        path.symlink_to(self.downloads / real.name)
        with self.assertRaises(ValueError):
            self.run_verify()
        path.unlink()
        (self.downloads / real.name).rename(path)
        directory = path.parent
        directory.rename(self.downloads / "saved-wheels")
        directory.symlink_to(self.downloads / "saved-wheels", target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_verify()

    def test_wrong_architecture_version_and_duplicate_inventory(self) -> None:
        with self.assertRaises(ValueError):
            verify(self.source, self.downloads, "linux/aarch64")
        self.closure["python"] = "3.12.14"
        self.pin()
        with self.assertRaises(ValueError):
            self.run_verify()
        self.closure["python"] = "3.12.3"
        self.closure["wheels"][1] = self.closure["wheels"][0]
        self.pin()
        with self.assertRaises(ValueError):
            self.run_verify()

    def test_changed_requirements_and_closure(self) -> None:
        (self.downloads / "requirements-documents.txt").write_text("reportlab\n")
        with self.assertRaises(ValueError):
            self.run_verify()
        self.pin()
        (self.source / "fleet/document-closure.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.run_verify()

    def test_rehashed_unpinned_requirements_rejected(self) -> None:
        self.pin()
        bad = b"reportlab>=4\n"
        self.closure["requirements_sha256"] = sha(bad)
        data = json.dumps(self.closure).encode()
        (self.source / "fleet/document-closure.json").write_bytes(data)
        (self.source / "requirements-documents.txt").write_bytes(bad)
        (self.source / "fleet/image-inputs.json").write_text(
            json.dumps(
                {
                    "documents": {
                        "closure": "fleet/document-closure.json",
                        "closure_sha256": sha(data),
                    }
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "requirements differ"):
            self.run_verify()

    def test_missing_notice_and_mismatched_metadata(self) -> None:
        self.wheel(0, missing_notice=True)
        self.pin()
        with self.assertRaises(KeyError):
            self.run_verify()
        self.wheel(0, version="0.0.0")
        self.pin()
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            self.run_verify()

    def test_unsafe_wheel_rejected_even_with_matching_hash(self) -> None:
        for name in ("../outside", "/absolute", "unsafe\\member"):
            self.wheel(0, extra=name)
            self.pin()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe"):
                self.run_verify()

    def test_installed_bytes_missing_changed_and_escaped(self) -> None:
        row = self.closure["wheels"][0]
        archive_path = self.downloads / "document-wheels" / row["filename"]
        prefix = Path(self.temp.name) / "synthetic-installed"
        with zipfile.ZipFile(archive_path) as archive:
            # Only our own synthetic test fixture is extracted, never real wheels.
            for name in archive.namelist():
                path = prefix / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
        dist = importlib.metadata.Distribution.at(prefix / "reportlab-4.4.9.dist-info")
        with patch(
            "runner_fleet.documents.importlib.metadata.distribution", return_value=dist
        ):
            verify_installed_wheel(archive_path, row, prefix)
            notice = prefix / row["notice_members"][0]
            original = notice.read_bytes()
            notice.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                verify_installed_wheel(archive_path, row, prefix)
            notice.unlink()
            with self.assertRaises(ValueError):
                verify_installed_wheel(archive_path, row, prefix)
            target = Path(self.temp.name) / "outside-notice"
            target.write_bytes(original)
            notice.symlink_to(target)
            with self.assertRaises(ValueError):
                verify_installed_wheel(archive_path, row, prefix)

    def test_document_inputs_are_in_controller_and_closure_identity(self) -> None:
        root = Path(self.temp.name) / "synthetic-controller"
        for name in (*FIXED, *RECIPE):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        initial = binding(root)
        for name in (
            "requirements-documents.txt",
            "fleet/document-closure.json",
            "fleet/DOCUMENT_NOTICES.md",
        ):
            path = root / name
            original = path.read_text()
            path.write_text(original + "changed")
            changed = binding(root)
            self.assertNotEqual(
                initial["controller_sha256"], changed["controller_sha256"]
            )
            self.assertNotEqual(
                initial["tool_closure_sha256"], changed["tool_closure_sha256"]
            )
            self.assertEqual(initial["recipe_sha256"], changed["recipe_sha256"])
            path.write_text(original)

    def test_repository_pins_and_preparation_boundaries(self) -> None:
        self.assertEqual(load(ROOT)["python"], "3.12.3")
        recipe = (ROOT / "tools/runner_fleet/prepare_image.sh").read_text()
        self.assertIn(
            "--no-index --no-deps --only-binary=:all: --require-hashes", recipe
        )
        self.assertIn("/opt/runspindle-documents/bin/python", recipe)
        self.assertIn("--installed", recipe)
        self.assertLess(
            recipe.index("runner_fleet.staging"), recipe.index("pip install")
        )
        self.assertIn(
            "document-wheels/", (ROOT / "tools/runner_fleet/build_image.py").read_text()
        )
        staging = (ROOT / "tools/runner_fleet/staging.py").read_text()
        self.assertEqual(staging.count("documents = verify_documents("), 1)
        self.assertIn(
            "v['documents']",
            (ROOT / "tools/runner_fleet/prepare_temporal.sh").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
