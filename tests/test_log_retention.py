"""Retain original diagnostics and fail closed before destructive cleanup."""

import base64
import errno
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet import backend
from tools.runner_fleet.retention import (
    capture_bytes,
    capture_file,
    snapshot_guest_disk,
)

Backend: Any = backend.Backend


class RetentionTests(unittest.TestCase):
    def fake_snapshot(
        self,
        disk: Path,
        base: Path,
        directory: Path,
        expected_base: str,
        run: Any,
        maximum_size: int = 24 * 1024**3,
    ) -> dict[str, Any]:
        """Orchestration seam only; real qcow2 behavior is tested separately."""
        retained = directory / "guest-retention-synthetic"
        retained.mkdir()
        snapshot = retained / "standalone.qcow2"
        snapshot.write_bytes(disk.read_bytes())
        original = retained / "original-overlay.qcow2"
        disk.rename(original)
        result = {
            "standalone": str(snapshot),
            "original": str(original),
            "observed_backing_image": str(base),
            "observed_backing_image_sha256": expected_base,
        }
        (retained / "verified-snapshot.json").write_text(json.dumps(result))
        return result

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
        self.enterContext(
            patch.object(backend, "snapshot_guest_disk", side_effect=self.fake_snapshot)
        )
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
            retained = json.loads(
                next(
                    directory.glob("guest-retention-*/verified-snapshot.json")
                ).read_text()
            )
            self.assertEqual(
                Path(retained["original"]).read_bytes(), b"complete disk.qcow2"
            )
            self.assertEqual(
                Path(retained["observed_backing_image"]).read_bytes(),
                b"synthetic backing bytes",
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
                next(
                    directory.glob("guest-retention-*/original-overlay.qcow2")
                ).read_bytes(),
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
            retained = next(directory.glob("guest-retention-*/original-overlay.qcow2"))
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


@unittest.skipUnless(
    shutil.which("qemu-img") and shutil.which("qemu-io"),
    "real qcow2 retention needs qemu-img and qemu-io; not a guest test",
)
class QcowRetentionTests(unittest.TestCase):
    def run_command(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, text=True, capture_output=True, check=False, timeout=20
        )

    def create_disk(self, root: Path, overlay_size: str = "1M") -> tuple[Path, Path]:
        raw = root / "synthetic.raw"
        raw.write_bytes(b"a" * 1024 * 1024)
        base = root / "base.qcow2"
        disk = root / "overlay.qcow2"
        for argv in (
            ["qemu-img", "convert", "-f", "raw", "-O", "qcow2", str(raw), str(base)],
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(base),
                str(disk),
                overlay_size,
            ],
            ["qemu-io", "-f", "qcow2", "-c", "write -P0x35 0 512", str(disk)],
        ):
            result = self.run_command(argv)
            self.assertEqual(result.returncode, 0, result.stderr)
        return disk, base

    def test_real_snapshot_is_independent_and_preserves_original_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk, base = self.create_disk(root)
            before = disk.read_bytes()
            inode = disk.stat().st_ino
            evidence = root / "evidence"
            evidence.mkdir()
            result = snapshot_guest_disk(
                disk,
                base,
                evidence,
                hashlib.sha256(base.read_bytes()).hexdigest(),
                self.run_command,
            )
            original = next(evidence.glob("guest-retention-*/original-overlay.qcow2"))
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(original.stat().st_ino, inode)
            base.rename(root / "preserved-base.qcow2")
            raw = root / "recovered.raw"
            recovered = self.run_command(
                [
                    "qemu-img",
                    "convert",
                    "-f",
                    "qcow2",
                    "-O",
                    "raw",
                    str(result["standalone"]),
                    str(raw),
                ]
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(raw.read_bytes(), b"5" * 512 + b"a" * (1024 * 1024 - 512))
            self.assertEqual(result["logical_compare"], "equal")
            self.assertFalse(disk.exists())

    def test_enlarged_overlay_retains_nonzero_extended_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk, base = self.create_disk(root, "2M")
            written = self.run_command(
                ["qemu-io", "-f", "qcow2", "-c", "write -P0x67 1048576 512", str(disk)]
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            evidence = root / "evidence"
            evidence.mkdir()
            result = snapshot_guest_disk(
                disk,
                base,
                evidence,
                hashlib.sha256(base.read_bytes()).hexdigest(),
                self.run_command,
            )
            raw = root / "recovered.raw"
            recovered = self.run_command(
                [
                    "qemu-img",
                    "convert",
                    "-f",
                    "qcow2",
                    "-O",
                    "raw",
                    str(result["standalone"]),
                    str(raw),
                ]
            )
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(result["virtual_size"], 2 * 1024**2)
            self.assertEqual(raw.read_bytes()[1024**2 : 1024**2 + 512], b"g" * 512)

    def test_external_base_data_file_dependency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base, disk = root / "base.qcow2", root / "overlay.qcow2"
            for argv in (
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-o",
                    f"data_file={root / 'external.raw'}",
                    str(base),
                    "1M",
                ],
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-F",
                    "qcow2",
                    "-b",
                    str(base),
                    str(disk),
                    "1M",
                ],
            ):
                result = self.run_command(argv)
                self.assertEqual(result.returncode, 0, result.stderr)
            evidence = root / "evidence"
            evidence.mkdir()
            with self.assertRaisesRegex(RuntimeError, "External data-file"):
                snapshot_guest_disk(
                    disk,
                    base,
                    evidence,
                    hashlib.sha256(base.read_bytes()).hexdigest(),
                    self.run_command,
                )
            self.assertTrue(disk.exists())
            self.assertTrue((root / "external.raw").exists())

    def test_timeout_preserves_partial_command_output_and_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk, base = self.create_disk(root)
            evidence = root / "evidence"
            evidence.mkdir()

            def timed_out(argv: list[str]) -> subprocess.CompletedProcess[str]:
                raise subprocess.TimeoutExpired(
                    argv, 1, output=b"partial stdout", stderr=b"partial stderr"
                )

            with self.assertRaises(subprocess.TimeoutExpired):
                snapshot_guest_disk(
                    disk,
                    base,
                    evidence,
                    hashlib.sha256(base.read_bytes()).hexdigest(),
                    timed_out,
                )
            logged = json.loads(
                next(evidence.glob("guest-retention-*/commands.log")).read_text()
            )
            self.assertEqual(logged["stdout"], "partial stdout")
            self.assertEqual(logged["stderr"], "partial stderr")
            self.assertTrue(disk.exists())

    def test_real_foreign_backing_chain_refuses_before_original_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk, base = self.create_disk(root)
            other = root / "other-base.qcow2"
            shutil.copyfile(base, other)
            evidence = root / "evidence"
            evidence.mkdir()
            with self.assertRaisesRegex(RuntimeError, "backing chain"):
                snapshot_guest_disk(
                    disk,
                    other,
                    evidence,
                    hashlib.sha256(other.read_bytes()).hexdigest(),
                    self.run_command,
                )
            self.assertTrue(disk.exists())
            self.assertFalse(list(evidence.glob("guest-retention-*/standalone.qcow2")))

    def test_corrupt_snapshot_is_detected_by_actual_logical_compare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            disk, base = self.create_disk(root)
            before = disk.read_bytes()
            evidence = root / "evidence"
            evidence.mkdir()

            def corrupt_copy(argv: list[str]) -> subprocess.CompletedProcess[str]:
                result = self.run_command(argv)
                if argv[1] == "convert" and result.returncode == 0:
                    changed = self.run_command(
                        ["qemu-io", "-f", "qcow2", "-c", "write -P0x77 0 512", argv[-1]]
                    )
                    self.assertEqual(changed.returncode, 0, changed.stderr)
                return result

            with self.assertRaisesRegex(RuntimeError, "command failed"):
                snapshot_guest_disk(
                    disk,
                    base,
                    evidence,
                    hashlib.sha256(base.read_bytes()).hexdigest(),
                    corrupt_copy,
                )
            self.assertEqual(disk.read_bytes(), before)
            self.assertEqual(
                len(list(evidence.glob("guest-retention-*/standalone.qcow2"))), 1
            )
            self.assertFalse(
                list(evidence.glob("guest-retention-*/verified-snapshot.json"))
            )


if __name__ == "__main__":
    unittest.main()
