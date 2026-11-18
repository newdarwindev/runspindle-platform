"""Real tiny qcow/SQLite fixtures; host stop and GitHub are always mocked."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.automatic_cleanup import (
    AutomaticCleanup,
    assert_unreferenced_image,
    base_references,
)
from runner_fleet.retention import file_digest
from runner_fleet.state import State


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = State(self.root / "state")
        self.addCleanup(self.state.db.close)
        self.base = self.root / "base.qcow2"
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(self.base), "1M"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        self.base.chmod(0o400)
        self.resource = self.state.reserve(
            {"repository": "runspindle-core", "id": 42},
            single_strix=True,
            details={
                "automatic_fleet_v1": True,
                "image_path": str(self.base),
                "image_sha256": file_digest(self.base),
            },
        )
        self.slot = self.root / "slots" / "0"
        self.slot.mkdir(parents=True, mode=0o700)
        subprocess.run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(self.base),
                str(self.slot / "disk.qcow2"),
                "1M",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        (self.slot / "disk.qcow2").chmod(0o600)
        for name in ("seed.iso", "serial.log"):
            p = self.slot / name
            p.write_bytes(b"synthetic")
            p.chmod(0o600)
        directory = self.state.directory / "resources" / self.resource["id"]
        directory.mkdir(parents=True, mode=0o700)
        (directory / "key").write_bytes(b"synthetic key")
        (directory / "key").chmod(0o600)
        self.archive = self.root / "retained" / self.resource["id"]
        self.archive.mkdir(parents=True, mode=0o700)
        self.stop = MagicMock()
        self.remove = MagicMock()
        self.cleanup = AutomaticCleanup(
            self.state, root=self.root, stop=self.stop, remove=self.remove
        )

    def test_success_preserves_original_and_protects_base_after_restart(self):
        inode = (self.slot / "disk.qcow2").stat().st_ino
        result = self.cleanup.finish(self.resource["id"])
        self.assertEqual(result["status"], "retained")
        self.assertEqual(self.state.active(), [])
        self.assertEqual((self.archive / "disk-original").stat().st_ino, inode)
        self.assertEqual(list(self.slot.iterdir()), [])
        self.stop.assert_called_once()
        self.remove.assert_called_once()
        reopened = State(self.state.directory)
        self.addCleanup(reopened.db.close)
        self.assertEqual(
            base_references(reopened), {str(self.base): file_digest(self.base)}
        )
        with self.assertRaises(ValueError):
            assert_unreferenced_image(reopened, self.base)

    def test_stop_failure_still_removes_registration_and_preserves_files(self):
        self.stop.side_effect = RuntimeError("uncertain stop")
        result = self.cleanup.finish(self.resource["id"])
        self.assertEqual(result["status"], "uncertain")
        self.remove.assert_called_once()
        self.assertTrue((self.slot / "disk.qcow2").exists())
        self.assertEqual(list(self.archive.iterdir()), [])
        self.assertEqual(self.state.active()[0]["phase"], "uncertain")

    def test_registration_failure_does_not_release_or_move(self):
        self.remove.side_effect = RuntimeError("API unavailable")
        self.assertEqual(
            self.cleanup.finish(self.resource["id"])["status"], "uncertain"
        )
        self.stop.assert_called_once()
        self.assertTrue((self.slot / "disk.qcow2").exists())

    def test_state_failure_does_not_skip_stop_or_removal(self):
        with patch.object(
            self.state, "update", side_effect=RuntimeError("database unavailable")
        ):
            with self.assertRaises(RuntimeError):
                self.cleanup.finish(self.resource["id"])
        self.stop.assert_called_once()
        self.remove.assert_called_once()
        self.assertEqual(list(self.archive.iterdir()), [])
        self.assertTrue(self.state.active())

    def test_existing_intent_is_not_blindly_retried(self):
        self.state.update(
            self.resource, "uncertain", original_retention={"partial": "fixture"}
        )
        self.assertEqual(
            self.cleanup.finish(self.resource["id"])["status"], "uncertain"
        )
        self.assertTrue((self.slot / "disk.qcow2").exists())
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_noncanonical_stored_or_queried_base_blocks_gc(self):
        alias = self.root / "alias.qcow2"
        alias.symlink_to(self.base)
        with self.assertRaises(ValueError):
            assert_unreferenced_image(self.state, alias)
        for path in [str(alias), str(self.root / "sub" / ".." / "base.qcow2")]:
            self.state.update(self.resource, "uncertain", image_path=path)
            with self.assertRaises(ValueError):
                base_references(self.state)
            with self.assertRaises(ValueError):
                assert_unreferenced_image(self.state, self.base)

    def test_foreign_resource_rejected_before_effects(self):
        with self.assertRaises(ValueError):
            self.cleanup.finish("f" * 32)
        self.stop.assert_not_called()
        self.remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
