"""Synthetic method-level evidence, never ARM64 installation qualification.

Fixtures deliberately bypass DisposableTarget construction. They exercise real
SQLite/readback code without invoking install, recover, rollback or a payload.
The native installation suite and its mandatory mutation gate remain separate.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools import development_bundle as development_module
from tools import streaming_bundle as streaming_module

# Legacy bundle APIs have dynamic JSON contracts; keep that boundary explicit.
development: Any = development_module
streaming: Any = streaming_module

ROOT = Path(__file__).resolve().parents[1]


class PortableInstallInvariants(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(
            "CREATE TABLE state(id,generation,active,pending);"
            "INSERT INTO state VALUES(1,1,'synthetic',NULL);"
            "CREATE TABLE installs(digest,bundle,state);"
        )

    def fixture(self, cls: Any = development.DisposableTarget) -> Any:
        # Explicit synthetic adapter: do not claim constructor/architecture proof.
        target = object.__new__(cls)
        target.root = self.root
        target.db = self.db
        return target

    def test_journal_family_rejection(self) -> None:
        for cls, own, foreign in (
            (development.DisposableTarget, "verified", "stream_verified"),
            (streaming.StreamingTarget, "stream_verified", "verified"),
        ):
            with self.subTest(family=cls.__name__):
                self.db.execute("DELETE FROM installs")
                self.db.execute(
                    "INSERT INTO installs VALUES('synthetic',NULL,?)", (own,)
                )
                target = self.fixture(cls)
                self.assertEqual(target.state()["generation"], 1)
                self.db.execute("UPDATE installs SET state=?", (foreign,))
                before = list(self.db.iterdump())
                with self.assertRaisesRegex(ValueError, "journal payload kind"):
                    target.state()
                self.assertEqual(list(self.db.iterdump()), before)

    def test_tree_corruption_and_modes(self) -> None:
        payload = self.root / "payload.txt"
        payload.write_bytes(b"synthetic data, never executed")
        payload.chmod(0o644)
        manifest = {
            "files": {
                "payload.txt": {
                    "sha256": streaming.file_sha(payload),
                    "size": payload.stat().st_size,
                    "mode": 0o644,
                }
            }
        }
        streaming.verify_tree(self.root, manifest)
        payload.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "installed byte or mode mismatch"):
            streaming.verify_tree(self.root, manifest)
        payload.chmod(0o644)
        payload.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "installed byte or mode mismatch"):
            streaming.verify_tree(self.root, manifest)

    def test_attribution_readback(self) -> None:
        pins = {
            name: development.sha(name.encode())[:40] for name in development.COMPONENTS
        }
        files = {"notice.txt": b"Synthetic original fixture"}
        bundle = development.build(
            development.VERSION,
            "a" * 40,
            pins,
            files,
            dependencies={"fixture": "sha256:" + "b" * 64},
            toolchains={"fixture": "sha256:" + "c" * 64},
            licenses={"notice.txt": development.sha(files["notice.txt"])},
        )
        digest = json.loads(bundle)["manifest_sha256"]
        folder = self.root / "versions" / digest
        folder.mkdir(parents=True)
        (folder / "notice.txt").write_bytes(files["notice.txt"])
        (self.root / "active.json").write_text(json.dumps({"manifest_sha256": digest}))
        self.db.execute("INSERT INTO installs VALUES(?,?,'verified')", (digest, bundle))
        # A negative admission test must still stop before the portable helper.
        # Never emulate a supported host to reach the native readback method.
        with (
            patch.object(development, "actual_target", return_value="unsupported"),
            patch.object(development, "_metadata_tree_readback") as readback,
        ):
            with self.assertRaisesRegex(ValueError, "wrong target architecture"):
                self.fixture().verify_installed(pins)
            readback.assert_not_called()
        # Manifest inspection and filesystem readback are portable. Do not patch
        # host identity or invoke a native installer to exercise this helper.
        manifest, expected_files, expected = development.verify(
            bundle, component_commits=pins, target="linux/arm64"
        )
        result = development._metadata_tree_readback(
            folder, manifest, expected_files, expected, digest
        )
        self.assertEqual(result["manifest_sha256"], digest)
        self.assertEqual(result["files"], len(files))
        self.assertEqual(result["unsupported"], development.UNSUPPORTED)
        with self.assertRaisesRegex(ValueError, "installed manifest identity mismatch"):
            development._metadata_tree_readback(
                folder, manifest, expected_files, expected, "0" * 64
            )
        (folder / "notice.txt").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "installed byte mismatch"):
            development._metadata_tree_readback(
                folder, manifest, expected_files, expected, digest
            )

    def test_production_constructor_still_rejects_unsupported_architecture(
        self,
    ) -> None:
        with patch.object(development, "actual_target", return_value="unsupported"):
            with self.assertRaisesRegex(ValueError, "unsupported execution target"):
                development.DisposableTarget.create()

    def test_portable_mutation_sensitivity(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                *(["-" + "O" * sys.flags.optimize] if sys.flags.optimize else []),
                str(ROOT / "tools/check_maintained_mutations.py"),
                "--suite",
                "portable",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(
            report["evidence_class"], "portable_component_mutation_detection"
        )
        self.assertEqual(report["native_acceptance"], "not_run")
        self.assertEqual(len(report["results"]), 5)
        self.assertTrue(all(item["detected"] for item in report["results"]))


if __name__ == "__main__":
    unittest.main()
