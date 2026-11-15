"""Retain original diagnostics and fail closed before destructive cleanup."""

import base64
import errno
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet import backend
from tools.runner_fleet.retention import capture_bytes, capture_file

Backend: Any = backend.Backend


class RetentionTests(unittest.TestCase):
    def test_repeated_capture_never_replaces_existing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = capture_bytes(root, "guest.log", b"first")
            second = capture_bytes(root, "guest.log", b"second")
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), b"first")
            self.assertEqual(second.read_bytes(), b"second")

    def test_linked_and_nonregular_log_sources_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"original")
            link = root / "linked.log"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                capture_file(link, root, "capture.log")
            fifo = root / "fifo.log"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(RuntimeError, "regular file"):
                capture_file(fifo, root, "capture.log")
            self.assertEqual(target.read_bytes(), b"original")
            self.assertFalse((root / "capture.log").exists())

    def fixture(self, root: Path) -> tuple[Any, dict[str, Any], Path, Path]:
        slot = root / "slots/0"
        slot.mkdir(parents=True)
        state = Mock(directory=root / "state")
        machine = Backend(state)
        base = root / "synthetic-base.qcow2"
        base.write_bytes(b"synthetic backing bytes")
        machine.image = base
        resource = {
            "id": "a" * 32,
            "slot": 0,
            "details": {"image_sha256": hashlib.sha256(base.read_bytes()).hexdigest()},
        }
        directory = machine.directory(resource)
        for name in ("disk.qcow2", "seed.iso", "serial.log"):
            (slot / name).write_bytes(b"complete " + name.encode())
        (directory / "key").write_bytes(b"synthetic-key")
        return machine, resource, slot, directory

    def test_destroy_retains_original_inode_and_earlier_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine, resource, slot, directory = self.fixture(root)
            serial = slot / "serial.log"
            original_inode = serial.stat().st_ino
            (directory / "serial.log").write_bytes(b"earlier prefix")
            with patch.object(backend, "ROOT", root), patch.object(backend, "helper"):
                machine.destroy(resource)
            self.assertEqual((directory / "serial.log").read_bytes(), b"earlier prefix")
            originals = list(directory.glob("serial-original-*.log"))
            self.assertEqual(len(originals), 1)
            self.assertEqual(originals[0].stat().st_ino, original_inode)
            self.assertEqual(originals[0].read_bytes(), b"complete serial.log")
            captures = list(directory.glob("serial.log.capture-*"))
            self.assertEqual(len(captures), 1)
            self.assertEqual(captures[0].read_bytes(), originals[0].read_bytes())
            self.assertEqual(list(slot.iterdir()), [])
            self.assertFalse((directory / "key").exists())
            retained = json.loads((directory / "retained-guest-disk.json").read_text())
            self.assertEqual(
                Path(retained["overlay"]).read_bytes(), b"complete disk.qcow2"
            )
            self.assertEqual(
                Path(retained["backing_image"]).read_bytes(), b"synthetic backing bytes"
            )

    def test_failed_verification_preserves_original_and_slot_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine, resource, slot, directory = self.fixture(root)
            incorrect = directory / "incorrect-capture.log"
            incorrect.write_bytes(b"incomplete")
            with (
                patch.object(backend, "ROOT", root),
                patch.object(backend, "helper"),
                patch.object(backend, "capture_file", return_value=incorrect),
                self.assertRaisesRegex(RuntimeError, "changed"),
            ):
                machine.destroy(resource)
            for name in ("seed.iso", "serial.log"):
                self.assertTrue((slot / name).exists())
            self.assertEqual(
                next(directory.glob("guest-disk-*.qcow2")).read_bytes(),
                b"complete disk.qcow2",
            )
            self.assertTrue((directory / "key").exists())

    def test_cross_filesystem_retention_has_no_copy_delete_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine, resource, slot, directory = self.fixture(root)
            with (
                patch.object(backend, "ROOT", root),
                patch.object(backend, "helper"),
                patch.object(
                    Path, "rename", side_effect=OSError(errno.EXDEV, "cross-device")
                ),
                self.assertRaises(OSError),
            ):
                machine.destroy(resource)
            self.assertEqual((slot / "serial.log").read_bytes(), b"complete serial.log")
            self.assertTrue((slot / "disk.qcow2").exists())
            self.assertTrue((directory / "key").exists())

    def test_successful_diagnostics_keeps_previous_text_and_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine, resource, slot, directory = self.fixture(root)
            old_text = directory / "guest-diagnostics.log"
            old_archive = directory / "guest-diagnostics.tar.gz"
            old_text.write_bytes(b"earlier text")
            old_archive.write_bytes(b"earlier archive")
            outputs = [
                subprocess.CompletedProcess([], 0, "new text", ""),
                subprocess.CompletedProcess(
                    [], 0, base64.b64encode(b"new archive").decode(), ""
                ),
            ]
            with (
                patch.object(backend, "ROOT", root),
                patch.object(machine, "ssh", side_effect=outputs),
                patch.object(backend, "execute"),
            ):
                machine.diagnostics(resource)
            self.assertEqual(old_text.read_bytes(), b"earlier text")
            self.assertEqual(old_archive.read_bytes(), b"earlier archive")
            self.assertEqual(
                next(directory.glob("guest-diagnostics.log.capture-*")).read_bytes(),
                b"new text",
            )
            self.assertEqual(
                next(directory.glob("guest-diagnostics.tar.gz.capture-*")).read_bytes(),
                b"new archive",
            )

    def test_guest_archive_failure_still_retains_complete_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine, resource, slot, directory = self.fixture(root)
            inode = (slot / "disk.qcow2").stat().st_ino
            with (
                patch.object(backend, "ROOT", root),
                patch.object(backend, "helper"),
                patch.object(
                    machine,
                    "ssh",
                    return_value=subprocess.CompletedProcess(
                        [], 255, "", "unavailable"
                    ),
                ),
                patch.object(backend, "execute"),
            ):
                machine.diagnostics(resource)
                machine.destroy(resource)
            retained = next(directory.glob("guest-disk-*.qcow2"))
            self.assertEqual(retained.stat().st_ino, inode)
            self.assertEqual(retained.read_bytes(), b"complete disk.qcow2")

    def test_registration_redacts_new_capture_and_preserves_previous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            machine, resource, slot, directory = self.fixture(root)
            resource["runner_name"] = "synthetic-runner"
            old = directory / "registration.log"
            old.write_bytes(b"earlier registration")
            with patch.object(
                machine,
                "ssh",
                return_value=subprocess.CompletedProcess(
                    [], 1, "token-value", "token-value"
                ),
            ):
                with self.assertRaises(RuntimeError):
                    machine.register(
                        resource, "synthetic/repo", "token-value", ["synthetic"]
                    )
            self.assertEqual(old.read_bytes(), b"earlier registration")
            new = next(directory.glob("registration.log.capture-*")).read_bytes()
            self.assertNotIn(b"token-value", new)
            self.assertEqual(new, b"[REDACTED][REDACTED]")


if __name__ == "__main__":
    unittest.main()
