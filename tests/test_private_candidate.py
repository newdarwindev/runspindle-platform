"""Private artifact admission and real tiny same-filesystem publication faults."""

import grp
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.runner_fleet import build_image as build


class PrivateCandidateTests(unittest.TestCase):
    def test_candidate_rejects_world_access_writers_wrong_owner_group_and_acl(
        self,
    ) -> None:
        path = Path("/synthetic/candidate")
        for mode, uid, gid, linked, acl, readable, accepted in (
            (0o440, 0, 975, False, [], True, True),
            (0o444, 0, 975, False, [], True, False),
            (0o640, 0, 975, False, [], True, False),
            (0o440, 1000, 975, False, [], True, False),
            (0o440, 0, 0, False, [], True, False),
            (0o440, 0, 975, True, [], True, False),
            (0o440, 0, 975, False, ["system.posix_acl_access"], True, False),
            (0o440, 0, 975, False, [], False, False),
        ):
            with (
                self.subTest(mode=mode, uid=uid, gid=gid, linked=linked, acl=acl),
                patch.object(
                    Path,
                    "stat",
                    return_value=SimpleNamespace(
                        st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=gid
                    ),
                ),
                patch.object(Path, "is_symlink", return_value=linked),
                patch.object(os, "listxattr", return_value=acl),
                patch.object(os, "access", return_value=readable),
            ):
                if accepted:
                    build.verify_private_candidate(path, 975)
                else:
                    with self.assertRaises(RuntimeError):
                        build.verify_private_candidate(path, 975)

    def test_staging_rejects_public_or_writable_directory_and_inherited_acl(
        self,
    ) -> None:
        for mode, acl in (
            (0o755, []),
            (0o770, []),
            (0o750, ["system.posix_acl_access"]),
            (0o750, ["system.posix_acl_default"]),
        ):
            with (
                self.subTest(mode=mode, acl=acl),
                patch.object(
                    Path,
                    "stat",
                    return_value=SimpleNamespace(
                        st_mode=stat.S_IFDIR | mode, st_uid=0, st_gid=975
                    ),
                ),
                patch.object(Path, "is_symlink", return_value=False),
                patch.object(os, "listxattr", return_value=acl),
                self.assertRaises(RuntimeError),
            ):
                build.verify_private_staging(Path("/synthetic/private"), 975)

    def test_active_group_required_before_state_or_guest_effects(self) -> None:
        with (
            patch.object(grp, "getgrnam", return_value=SimpleNamespace(gr_gid=975)),
            patch.object(os, "getgid", return_value=1000),
            patch.object(os, "getgroups", return_value=[1000]),
            patch.object(build, "require_build_capacity"),
            patch.object(build, "require_build_runtime"),
            patch.object(build, "require_build_storage"),
            patch.object(build, "State") as state,
            patch.object(build, "execute") as execute,
            self.assertRaisesRegex(SystemExit, "must be active"),
        ):
            build.main()
        state.assert_not_called()
        execute.assert_not_called()

    def test_existing_active_membership_passes_without_changing_membership(
        self,
    ) -> None:
        with (
            patch.object(grp, "getgrnam", return_value=SimpleNamespace(gr_gid=975)),
            patch.object(os, "getgid", return_value=1000),
            patch.object(os, "getgroups", return_value=[1000, 975]),
        ):
            self.assertEqual(build.require_build_group(), 975)

    def test_real_no_clobber_skip_is_failure_with_both_originals_preserved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "target"
            source.write_bytes(b"new synthetic image")
            target.write_bytes(b"existing synthetic image")

            def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
                # Only these test-owned tiny files; never invoke sudo in tests.
                return subprocess.run(
                    argv[2:], check=True, capture_output=True, text=True
                )

            with (
                patch.object(build, "verify_private_candidate"),
                patch.object(build, "execute", side_effect=run),
                self.assertRaisesRegex(RuntimeError, "install uncertain"),
            ):
                build.publish_private_candidate(source, target, 975)
            self.assertEqual(source.read_bytes(), b"new synthetic image")
            self.assertEqual(target.read_bytes(), b"existing synthetic image")

    def test_real_publication_preserves_original_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "target"
            source.write_bytes(b"synthetic image")
            inode = source.stat().st_ino

            def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    argv[2:], check=True, capture_output=True, text=True
                )

            with (
                patch.object(build, "verify_private_candidate"),
                patch.object(build, "execute", side_effect=run),
            ):
                build.publish_private_candidate(source, target, 975)
            self.assertFalse(source.exists())
            self.assertEqual(target.stat().st_ino, inode)
            self.assertEqual(target.read_bytes(), b"synthetic image")


if __name__ == "__main__":
    unittest.main()
