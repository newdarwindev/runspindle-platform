"""Actual tiny synthetic qcow images; no production guest or data copied."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.original_retention import (
    inspect_linked_disk,
    retain_originals,
    verify_backing_binding,
)
from runner_fleet.retention import file_digest


class OriginalRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "base.qcow2"
        self.disk = self.root / "disk.qcow2"
        self.command(["qemu-img", "create", "-f", "qcow2", str(self.base), "1M"])
        self.base.chmod(0o400)
        self.command(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(self.base),
                str(self.disk),
                "1M",
            ]
        )
        self.disk.chmod(0o600)
        self.files = {"disk": self.disk}
        for name in ["seed", "serial", "key"]:
            path = self.root / name
            path.write_bytes(("synthetic-" + name).encode())
            path.chmod(0o600)
            self.files[name] = path
        self.archive = self.root / "retained"
        self.archive.mkdir(mode=0o700)
        self.binding = inspect_linked_disk(
            self.disk, self.base, file_digest(self.base), self.command
        )

    @staticmethod
    def command(argv):
        return subprocess.run(
            argv, check=True, text=True, capture_output=True, timeout=10
        )

    def test_original_inodes_preserved_no_copy(self):
        before = {k: p.stat().st_ino for k, p in self.files.items()}
        phases = []
        receipt = retain_originals(
            self.files,
            self.archive,
            self.binding,
            lambda phase, data: phases.append(phase),
        )
        self.assertEqual(phases, ["intent", "complete"])
        for name, row in receipt["files"].items():
            self.assertFalse(Path(row["source"]).exists())
            self.assertEqual(Path(row["destination"]).stat().st_ino, before[name])
        self.assertEqual(len(list(self.archive.iterdir())), 4)
        verify_backing_binding(self.binding)

    def test_failed_intent_never_moves(self):
        def fail(phase, data):
            raise RuntimeError("database unavailable")

        with self.assertRaises(RuntimeError):
            retain_originals(self.files, self.archive, self.binding, fail)
        self.assertTrue(all(p.exists() for p in self.files.values()))
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_interrupted_partial_move_preserves_every_original(self):
        phases = []

        def change(phase, data):
            phases.append(phase)
            if phase == "intent":
                self.files["key"].write_bytes(b"changed synthetic")

        with self.assertRaises(ValueError):
            retain_originals(self.files, self.archive, self.binding, change)
        self.assertEqual(phases, ["intent"])
        for name, path in self.files.items():
            self.assertEqual(
                int(path.exists())
                + int((self.archive / (name + "-original")).exists()),
                1,
            )

    def test_occupied_destination_and_hardlink_refused(self):
        (self.archive / "disk-original").write_bytes(b"preserve")
        with self.assertRaises(ValueError):
            retain_originals(self.files, self.archive, self.binding, lambda *_: None)
        self.assertEqual((self.archive / "disk-original").read_bytes(), b"preserve")
        os.link(self.disk, self.root / "another-name")
        with self.assertRaises(ValueError):
            inspect_linked_disk(
                self.disk, self.base, file_digest(self.base), self.command
            )

    def test_changed_base_never_reports_complete(self):
        phases = []

        def change(phase, data):
            phases.append(phase)
            if phase == "intent":
                self.base.chmod(0o600)

        with self.assertRaises(ValueError):
            retain_originals(self.files, self.archive, self.binding, change)
        self.assertEqual(phases, ["intent"])
        with self.assertRaises(ValueError):
            verify_backing_binding(self.binding)

    def test_wrong_base_or_symlink_rejected(self):
        with self.assertRaises(ValueError):
            inspect_linked_disk(self.disk, self.base, "a" * 64, self.command)
        link = self.root / "linked.qcow2"
        link.symlink_to(self.disk)
        with self.assertRaises(ValueError):
            inspect_linked_disk(link, self.base, file_digest(self.base), self.command)


if __name__ == "__main__":
    unittest.main()
