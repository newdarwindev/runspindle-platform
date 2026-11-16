"""Temporary synthetic filesystem tests; never run the guest-only sealer on host."""

from __future__ import annotations

import errno
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from tools.runner_fleet import seal_preparation_guest as seal


class SealPreparationTests(unittest.TestCase):
    def fixture(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        cloud = root / "cloud"
        (cloud / "instances" / "synthetic").mkdir(parents=True)
        (cloud / "instances" / "synthetic" / "user-data").write_bytes(
            b"synthetic private input"
        )
        (cloud / "instance").symlink_to("instances/synthetic")
        evidence = root / "private-evidence"
        evidence.mkdir(mode=0o700)
        return cloud, evidence

    def test_cloud_original_inodes_links_and_bytes_retained_before_fresh_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cloud, evidence = self.fixture(pathlib.Path(directory))
            expected = seal.tree_inventory(cloud)
            receipt = seal.archive_cloud_metadata(cloud, evidence)
            self.assertTrue(receipt["verified_same_inodes_and_bytes"])
            self.assertEqual(seal.tree_inventory(evidence / "cloud-original"), expected)
            self.assertEqual(list(cloud.iterdir()), [])
            self.assertEqual(
                (evidence / "cloud-original" / "instance").readlink(),
                pathlib.Path("instances/synthetic"),
            )

    def test_cross_filesystem_rename_failure_keeps_source_without_copy_delete_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cloud, evidence = self.fixture(pathlib.Path(directory))
            expected = seal.tree_inventory(cloud)
            with patch(
                "os.rename",
                side_effect=OSError(errno.EXDEV, "synthetic cross-filesystem denial"),
            ):
                with self.assertRaises(OSError):
                    seal.archive_cloud_metadata(cloud, evidence)
            self.assertEqual(seal.tree_inventory(cloud), expected)
            self.assertFalse((evidence / "cloud-original").exists())

    def test_inventory_mismatch_retains_original_and_blocks_fresh_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cloud, evidence = self.fixture(pathlib.Path(directory))
            real_inventory = seal.tree_inventory
            count = 0

            def observed(path: pathlib.Path) -> dict[str, dict[str, object]]:
                nonlocal count
                count += 1
                inventory = real_inventory(path)
                if count == 2:
                    inventory["synthetic-change"] = {"kind": "changed"}
                return inventory

            with patch.object(seal, "tree_inventory", side_effect=observed):
                with self.assertRaisesRegex(RuntimeError, "cloud metadata changed"):
                    seal.archive_cloud_metadata(cloud, evidence)
            self.assertTrue(
                (
                    evidence / "cloud-original" / "instances/synthetic/user-data"
                ).is_file()
            )
            self.assertFalse(cloud.exists())

    def test_guest_keys_and_debug_tree_preserve_originals_privately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            evidence = root / "private"
            evidence.mkdir(mode=0o700)
            key = root / "ssh_host_synthetic_key"
            key.write_bytes(b"synthetic key")
            original_inode = key.stat().st_ino
            receipt = seal.archive_identity_file(key, evidence, key.name)
            self.assertTrue(receipt["same_inode"])
            self.assertEqual((evidence / key.name).stat().st_ino, original_inode)
            debug = root / "debugbuild"
            debug.mkdir()
            (debug / "build.log").write_bytes(b"synthetic build diagnostics")
            seal.archive_debug_tree(debug, evidence)
            self.assertFalse(debug.exists())
            self.assertEqual(
                (evidence / "debugbuild-original/build.log").read_bytes(),
                b"synthetic build diagnostics",
            )
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o700)

    def test_debug_cross_filesystem_input_blocks_without_any_move(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            debug = root / "debugbuild"
            debug.mkdir()
            (debug / "synthetic.log").write_bytes(b"synthetic original")
            evidence = root / "private"
            evidence.mkdir(mode=0o700)
            original_stat = pathlib.Path.stat

            def different_device(
                path: pathlib.Path, *args: object, **kwargs: object
            ) -> os.stat_result:
                info = original_stat(path)
                if path == debug:
                    values = list(info)
                    values[2] += 1
                    return os.stat_result(values)
                return info

            with patch.object(pathlib.Path, "stat", different_device):
                with self.assertRaisesRegex(
                    RuntimeError, "debug retention crosses filesystems"
                ):
                    seal.archive_debug_tree(debug, evidence)
            self.assertEqual(
                (debug / "synthetic.log").read_bytes(), b"synthetic original"
            )
            self.assertFalse((evidence / "debugbuild-original").exists())

    def test_guest_script_preserves_logs_before_terminal_image_emission(self) -> None:
        script = (
            pathlib.Path(__file__).resolve().parents[1]
            / "tools/runner_fleet/prepare_temporal.sh"
        ).read_text()
        self.assertNotIn("cloud-init clean --logs", script)
        self.assertNotIn("rm -rf /tmp/debugbuild", script)
        self.assertNotIn("--rm", script)
        self.assertIn("export RUNSPINDLE_TEMPORAL_RETAIN=1", script)
        sealer = script.index(
            "sudo python3 /opt/runspindle-image-tools/tools/runner_fleet/seal_preparation_guest.py"
        )
        clean = script.index("sudo cloud-init clean --machine-id")
        terminal = script.index("printf 'RUNSPINDLE_IMAGE='")
        self.assertLess(sealer, clean)
        self.assertLess(clean, terminal)

    def test_linked_identity_input_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            target = root / "original"
            target.write_bytes(b"synthetic original")
            linked = root / "linked"
            linked.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "linked or nonregular"):
                seal.archive_identity_file(linked, root, "retained")
            self.assertEqual(target.read_bytes(), b"synthetic original")

    def test_guest_guard_rejects_nonroot_and_wrong_marker_authority(self) -> None:
        marker = {
            "preparation": True,
            "slot": 0,
            "resource": "a" * 32,
            "image_sha256": "b" * 64,
        }
        self.assertEqual(
            seal.validate_marker(json.dumps(marker).encode(), 0, 0o644), marker
        )
        for change in ({"preparation": False}, {"slot": True}, {"resource": "host"}):
            with self.assertRaises(RuntimeError):
                seal.validate_marker(json.dumps(marker | change).encode(), 0, 0o644)
        with self.assertRaises(RuntimeError):
            seal.validate_marker(json.dumps(marker).encode(), os.getuid() + 1, 0o666)
        with patch(
            "tools.runner_fleet.seal_preparation_guest.os.geteuid", return_value=1000
        ):
            with self.assertRaisesRegex(RuntimeError, "guest-root only"):
                seal.require_guest_root()

    def test_machine_id_preflight_rejects_links_and_special_files_before_mutation(
        self,
    ) -> None:
        for kind in ("symlink", "broken-symlink", "directory", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                path = root / "machine-id"
                original = root / "original"
                original.write_bytes(b"synthetic original identity")
                if kind == "symlink":
                    path.symlink_to(original)
                elif kind == "broken-symlink":
                    path.symlink_to(root / "absent")
                elif kind == "directory":
                    path.mkdir()
                else:
                    os.mkfifo(path)
                evidence = root / "not-created"
                with (
                    patch.object(seal, "MACHINE_ID", path),
                    patch.object(
                        seal,
                        "require_guest_root",
                        return_value={"synthetic": "guest-marker"},
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "machine-id refused before sealing mutations"
                    ):
                        seal.preparation_preflight()
                self.assertFalse(evidence.exists())
                self.assertEqual(original.read_bytes(), b"synthetic original identity")
                self.assertTrue(path.exists() or path.is_symlink())

    def test_regular_or_absent_machine_id_preflight_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "machine-id"
            self.assertIsNone(seal.machine_id_identity(path))
            path.write_bytes(b"synthetic identity")
            before = path.stat()
            identity = seal.machine_id_identity(path)
            self.assertIsNotNone(identity)
            self.assertEqual(path.read_bytes(), b"synthetic identity")
            self.assertEqual(path.stat().st_ino, before.st_ino)

    def test_prior_evidence_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            seal.private_capture(root, "receipt.json", b"synthetic original")
            with self.assertRaises(FileExistsError):
                seal.private_capture(root, "receipt.json", b"replacement")
            self.assertEqual(
                (root / "receipt.json").read_bytes(), b"synthetic original"
            )
