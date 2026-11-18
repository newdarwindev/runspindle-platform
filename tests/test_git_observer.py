"""Real tiny local Git repositories, never network or runner qualification."""

import dataclasses
import os
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from tools.runner_fleet import git_observer as observer
from tools.runner_fleet.environment_applicability import (
    Observation,
    Profile,
    compatible,
)


class ObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "setup.py").write_text("setup\n")
        (self.root / "app.txt").write_text("app\n")
        (self.root / ".gitignore").write_text("ignored*\n")
        self.commit()

    def git(self, *args: str) -> str:
        return (
            subprocess.check_output(
                ["/usr/bin/git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )

    def commit(self) -> None:
        self.git("add", ".")
        self.git("commit", "-qm", "synthetic fixture")
        self.sha = self.git("rev-parse", "HEAD")

    def observe(self) -> Observation:
        return observer.observe_checkout(
            self.root, "fixture/repo", self.sha, "a" * 64, "b" * 64
        )

    def test_complete_tree_feeds_disconnected_policy(self) -> None:
        first = self.observe()
        profile = Profile(
            "fixture/repo",
            "a" * 64,
            "b" * 64,
            tuple(b for b in first.blobs if b.path != "app.txt"),
            ("app.txt",),
            (),
        )
        (self.root / "app.txt").write_text("new app\n")
        self.commit()
        second = self.observe()
        self.assertNotEqual(first.source_sha, second.source_sha)
        self.assertEqual(compatible(profile, first), compatible(profile, second))
        (self.root / "setup.py").write_text("changed setup\n")
        self.commit()
        with self.assertRaises(ValueError):
            compatible(profile, self.observe())

    def test_dirty_and_assume_unchanged_rejected(self) -> None:
        self.git("update-index", "--assume-unchanged", "app.txt")
        (self.root / "app.txt").write_text("dirty\n")
        with self.assertRaises(ValueError):
            self.observe()

    def test_extra_ignored_and_untracked_inputs_rejected(self) -> None:
        for name in ["ignored.py", "new.txt"]:
            with self.subTest(name=name):
                (self.root / name).write_text("extra\n")
                with self.assertRaises(ValueError):
                    self.observe()
                (self.root / name).unlink()

    def test_extra_empty_directory_is_an_input_and_rejected(self) -> None:
        (self.root / "nested").mkdir()
        (self.root / "nested" / "tracked.txt").write_text("tracked\n")
        self.commit()
        self.observe()
        for name in ("feature", "ignored_directory", "nested/empty"):
            directory = self.root / name
            directory.mkdir()
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "directory"),
            ):
                self.observe()
            directory.rmdir()
        self.observe()

    def test_index_and_wrong_commit_rejected(self) -> None:
        (self.root / "app.txt").write_text("staged\n")
        self.git("add", "app.txt")
        (self.root / "app.txt").write_text("app\n")
        with self.assertRaises(ValueError):
            self.observe()
        self.git("reset", "-q", "HEAD")
        self.sha = "0" * 40
        with self.assertRaises(ValueError):
            self.observe()

    def test_symlink_gitlink_and_fifo_rejected(self) -> None:
        (self.root / "link").symlink_to("app.txt")
        self.commit()
        with self.assertRaises(ValueError):
            self.observe()
        self.git("rm", "-q", "link")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.sha},module")
        self.git("commit", "-qm", "synthetic gitlink")
        self.sha = self.git("rev-parse", "HEAD")
        with self.assertRaises(ValueError):
            self.observe()
        self.git("rm", "--cached", "-q", "module")
        self.commit()
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(ValueError):
            self.observe()

    def test_modes_missing_and_bounds(self) -> None:
        app = self.root / "app.txt"
        app.chmod(0o755)
        with self.assertRaises(ValueError):
            self.observe()
        app.chmod(0o644)
        for name, limit in [
            ("MAX_FILES", 1),
            ("MAX_TREE_BYTES", 1),
            ("MAX_FILE_BYTES", 1),
            ("MAX_SECONDS", -1),
        ]:
            with patch.object(observer, name, limit), self.assertRaises(ValueError):
                self.observe()
        app.unlink()
        with self.assertRaises(ValueError):
            self.observe()

    def test_nested_tree_and_corrupt_object_denied(self) -> None:
        (self.root / "nested").mkdir()
        (self.root / "nested" / "input.txt").write_text("nested input\n")
        self.commit()
        self.assertEqual(len(self.observe().blobs), 4)
        oid = self.git("rev-parse", "HEAD:nested/input.txt")
        obj = self.root / ".git" / "objects" / oid[:2] / oid[2:]
        obj.chmod(0o644)
        obj.write_bytes(zlib.compress(b"blob 13\0corrupt data\n"))
        with self.assertRaises(ValueError):
            self.observe()

    def test_git_output_bound_and_no_replace_object(self) -> None:
        with self.assertRaises(ValueError):
            observer._git(self.root, ["rev-parse", "HEAD"], 1, time.monotonic() + 5)
        original = self.sha
        (self.root / "app.txt").write_text("replacement\n")
        self.commit()
        replacement = self.sha
        self.git("checkout", "-q", original)
        self.git("replace", original, replacement)
        self.sha = original
        result = self.observe()
        self.assertEqual(result.source_sha, original)
        self.assertNotEqual(result, dataclasses.replace(result, source_sha=replacement))


if __name__ == "__main__":
    unittest.main()
