"""Synthetic method-level evidence, never ARM64 installation qualification.

Fixtures deliberately bypass DisposableTarget construction. They exercise real
SQLite/readback code without invoking install, recover, rollback or a payload.
The native installation suite and its mandatory mutation gate remain unchanged.
"""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
import unittest
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
        target = self.fixture()
        # Only the method's manifest target input is synthetic. This is not a
        # host architecture observation, constructor test or installed receipt.
        with patch.object(development, "actual_target", return_value="linux/arm64"):
            result = target.verify_installed(pins)
            self.assertEqual(result["manifest_sha256"], digest)
            self.assertEqual(result["files"], len(files))
            (folder / "notice.txt").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "installed byte mismatch"):
                target.verify_installed(pins)

    def test_production_constructor_still_rejects_unsupported_architecture(
        self,
    ) -> None:
        with patch.object(development, "actual_target", return_value="unsupported"):
            with self.assertRaisesRegex(ValueError, "unsupported execution target"):
                development.DisposableTarget.create()

    def test_portable_mutation_sensitivity(self) -> None:
        cases = (
            (
                "development_bundle.py",
                "def state(self):\n        self._check_journal_kind()",
                "def state(self):\n        pass",
                "test_journal_family_rejection",
            ),
            (
                "streaming_bundle.py",
                'check(actual == manifest["files"], "installed byte or mode mismatch")',
                'check(True, "installed byte or mode mismatch")',
                "test_tree_corruption_and_modes",
            ),
            (
                "development_bundle.py",
                '"manifest_sha256": digest,\n            "files": len(files),',
                '"manifest_sha256": "0" * 64,\n            "files": len(files),',
                "test_attribution_readback",
            ),
        )
        for filename, old, new, method in cases:
            with (
                self.subTest(mutation=method),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                shutil.copytree(
                    ROOT / "tools",
                    root / "tools",
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                shutil.copyfile(__file__, root / Path(__file__).name)
                source = root / "tools" / filename
                text = source.read_text()
                self.assertEqual(text.count(old), 1)
                source.write_text(text.replace(old, new))
                env = {
                    **os.environ,
                    "PYTHONPATH": str(root),
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "unittest",
                        f"test_portable_install_invariants.PortableInstallInvariants.{method}",
                        "-v",
                    ],
                    cwd=root,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("FAIL:", result.stderr)
                self.assertNotIn("ERROR:", result.stderr)
                self.assertNotIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
