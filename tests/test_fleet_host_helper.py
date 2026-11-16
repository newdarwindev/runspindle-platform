"""A stale cleanup request must never affect a replacement guest."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet import host_helper


class OwnershipTests(unittest.TestCase):
    def test_stale_stop_rejected_before_external_operation(self):
        with (
            patch.object(
                host_helper, "observe", return_value={"resource_id": "b" * 32}
            ),
            patch.object(host_helper, "run") as external,
        ):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                host_helper.stop(0, "a" * 32)
            external.assert_not_called()

    def test_unconfirmed_stop_never_prepares_archive(self) -> None:
        observations = [
            {"resource_id": "a" * 32},
            {"ActiveState": "active", "MainPID": "123"},
        ]
        with (
            patch.object(host_helper, "observe", side_effect=observations),
            patch.object(host_helper, "run"),
            patch.object(host_helper, "prepare_original_archive") as archive,
        ):
            with self.assertRaisesRegex(RuntimeError, "termination uncertain"):
                host_helper.stop(0, "a" * 32)
            archive.assert_not_called()

    def test_private_archive_is_fixed_scope_and_refuses_links_or_permissions(
        self,
    ) -> None:
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_lstat = Path.lstat

            def root_parent(path: Path) -> SimpleNamespace:
                value = original_lstat(path)
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_uid=0 if path == root / "retained" else value.st_uid,
                    st_gid=value.st_gid,
                )

            with (
                patch.object(host_helper, "ROOT", root),
                patch.object(Path, "lstat", root_parent),
                patch.object(host_helper.os, "chown"),
            ):
                archive = host_helper.prepare_original_archive(
                    "a" * 32, os.getuid(), os.getgid()
                )
                marker = archive / "preserved.log"
                marker.write_bytes(b"complete original")
                self.assertEqual(
                    host_helper.prepare_original_archive(
                        "a" * 32, os.getuid(), os.getgid()
                    ),
                    archive,
                )
                archive.chmod(0o755)
                with self.assertRaisesRegex(RuntimeError, "Unsafe private"):
                    host_helper.prepare_original_archive(
                        "a" * 32, os.getuid(), os.getgid()
                    )
                self.assertEqual(marker.read_bytes(), b"complete original")
                with self.assertRaisesRegex(RuntimeError, "Invalid retention"):
                    host_helper.prepare_original_archive(
                        "../foreign", os.getuid(), os.getgid()
                    )
                outside = root / "outside"
                outside.mkdir()
                (root / "retained" / ("b" * 32)).symlink_to(
                    outside, target_is_directory=True
                )
                with self.assertRaisesRegex(RuntimeError, "Unsafe private"):
                    host_helper.prepare_original_archive(
                        "b" * 32, os.getuid(), os.getgid()
                    )
                self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
