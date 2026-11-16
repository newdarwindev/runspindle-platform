"""Preparation uses immutable Git blobs, not a changing worktree."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet import backend, build_image


class FrozenSourceTests(unittest.TestCase):
    def test_committed_blob_survives_worktree_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", "--template=", str(root)], check=True
            )
            (root / "prepare.sh").write_text("frozen preparation\n")
            subprocess.run(["git", "-C", str(root), "add", "prepare.sh"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.logAllRefUpdates=false",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "user.name=Synthetic",
                    "-c",
                    "user.email=synthetic@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "synthetic fixture",
                ],
                check=True,
            )
            revision = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            (root / "prepare.sh").write_text("changed working copy\n")
            self.assertEqual(
                backend.frozen_preparation_text(root, revision, "prepare.sh"),
                "frozen preparation\n",
            )
            self.assertEqual(
                (root / "prepare.sh").read_text(), "changed working copy\n"
            )

    def test_unsafe_source_refuses_before_git_or_guest_effects(self) -> None:
        with patch.object(subprocess, "check_output") as git:
            for revision, relative in (
                ("main", "prepare.sh"),
                ("a" * 40, "../outside"),
                ("a" * 40, "/absolute"),
            ):
                with self.assertRaises(RuntimeError):
                    backend.frozen_preparation_text(
                        Path("/synthetic"), revision, relative
                    )
            git.assert_not_called()
        state = Mock()
        machine: Any = backend.Backend(state)
        with patch.object(backend, "helper") as helper:
            with self.assertRaisesRegex(RuntimeError, "before effects"):
                machine.create({"id": "synthetic", "slot": 0}, prepare=True)
            helper.assert_not_called()
            state.update.assert_not_called()

    def test_source_drift_cannot_emit_qualified_build(self) -> None:
        expected = {"controller_sha256": "a" * 64}
        with (
            patch.object(
                subprocess,
                "check_output",
                side_effect=["b" * 40, " M prepare.sh"],
            ),
            patch.object(build_image, "binding", return_value=expected),
            self.assertRaisesRegex(RuntimeError, "source drift"),
        ):
            build_image.verify_build_source("b" * 40, expected)
        with (
            patch.object(subprocess, "check_output", side_effect=["b" * 40, ""]),
            patch.object(
                build_image, "binding", return_value={"controller_sha256": "c" * 64}
            ),
            self.assertRaisesRegex(RuntimeError, "source drift"),
        ):
            build_image.verify_build_source("b" * 40, expected)


if __name__ == "__main__":
    unittest.main()
