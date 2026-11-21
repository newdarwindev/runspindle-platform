"""Read-only inventory checks against real extracted development payloads."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools import check_maintained_mutations as mutations
from tools import streaming_bundle as bundle_module
from tools.development_bundle import COMPONENTS

bundle: Any = bundle_module
PINS = {name: hashlib.sha256(name.encode()).hexdigest()[:40] for name in COMPONENTS}


class InstalledStreamingInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "source"
        self.source.write_bytes(b"synthetic executable bytes")
        notice = root / "NOTICE"
        notice.write_bytes(b"synthetic notice")
        archive = root / "bundle"
        self.manifest, _ = bundle.build(
            archive,
            "a" * 40,
            PINS,
            {"bin/tool": self.source, "licenses/LICENSE": notice},
            dependencies={"fixture": "sha256:" + hashlib.sha256(b"dep").hexdigest()},
            toolchains={"fixture": "sha256:" + hashlib.sha256(b"tool").hexdigest()},
            licenses={
                "licenses/LICENSE": hashlib.sha256(notice.read_bytes()).hexdigest()
            },
            entrypoints={"tool": "bin/tool"},
            executable=("bin/tool",),
        )
        self.tree = root / "installed"
        self.tree.mkdir()
        bundle.inspect(archive, component_commits=PINS, destination=self.tree)
        self.installed = self.tree / "bin/tool"

    def test_unexpected_entries_reject_before_hashing_any_installed_file(self) -> None:
        extra = self.tree / "cache"
        extra.mkdir()
        with patch.object(bundle, "file_sha", wraps=bundle.file_sha) as hashed:
            with self.assertRaisesRegex(ValueError, "unexpected installed directory"):
                bundle.verify_tree(self.tree, self.manifest)
            hashed.assert_not_called()
            extra.rmdir()
            extra.write_bytes(b"unexpected content")
            with self.assertRaisesRegex(ValueError, "unexpected installed file"):
                bundle.verify_tree(self.tree, self.manifest)
            hashed.assert_not_called()
        extra.unlink()
        with patch.object(bundle, "file_sha", wraps=bundle.file_sha) as hashed:
            bundle.verify_tree(self.tree, self.manifest)
            self.assertEqual(hashed.call_count, len(self.manifest["files"]))
            for name, record in self.manifest["files"].items():
                hashed.assert_any_call(self.tree / name, limit=record["size"])

    def test_missing_or_oversized_expected_file_rejects_before_hashing(self) -> None:
        with patch.object(bundle, "file_sha", wraps=bundle.file_sha) as hashed:
            self.installed.unlink()
            with self.assertRaisesRegex(ValueError, "installed byte or mode mismatch"):
                bundle.verify_tree(self.tree, self.manifest)
            self.installed.write_bytes(self.source.read_bytes() + b"x")
            self.installed.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "installed byte or mode mismatch"):
                bundle.verify_tree(self.tree, self.manifest)
            hashed.assert_not_called()

    def test_equivalent_hardlink_rejects_and_same_size_corruption_is_detected(
        self,
    ) -> None:
        self.installed.unlink()
        self.source.chmod(0o755)
        os.link(self.source, self.installed)
        with patch.object(bundle, "file_sha", wraps=bundle.file_sha) as hashed:
            with self.assertRaisesRegex(ValueError, "unsafe installed link"):
                bundle.verify_tree(self.tree, self.manifest)
            hashed.assert_not_called()
        self.installed.unlink()
        original = self.source.read_bytes()
        self.installed.write_bytes(b"X" + original[1:])
        self.installed.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "installed byte or mode mismatch"):
            bundle.verify_tree(self.tree, self.manifest)
        self.assertEqual(self.source.read_bytes(), original)

    def test_removing_directory_admission_fails_the_independent_test(self) -> None:
        self.assertTrue(
            mutations.run_case(
                "streaming_directory_inventory_ignored",
                (
                    "streaming_bundle.py",
                    "index < len(names) and names[index].startswith(child)",
                    "True",
                    "test_installed_stream_inventory.InstalledStreamingInventoryTests."
                    "test_unexpected_entries_reject_before_hashing_any_installed_file",
                ),
                "portable",
            )
        )
