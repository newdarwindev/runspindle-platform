"""Real byte publication and portable archive staging, independent of install target."""

import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools import streaming_bundle as streaming_module
from tools.development_bundle import COMPONENTS

bundle: Any = streaming_module
ROOT = Path(__file__).resolve().parents[1]
PINS = {name: bundle.sha(name.encode())[:40] for name in COMPONENTS}
BUILD_WORKER = r"""
from pathlib import Path
import sys
import time
from unittest.mock import patch
from tools import streaming_bundle as bundle
from tools.development_bundle import COMPONENTS

root, name = Path(sys.argv[1]), sys.argv[2]
source = root / name
source.write_bytes(name.encode())
pins = {part: bundle.sha(part.encode())[:40] for part in COMPONENTS}
original = bundle.inspect
def verify(path, **options):
    result = original(path, **options)
    (root / (name + '.ready')).touch()
    stop = time.monotonic() + 8
    while not (root / 'continue').is_file() and time.monotonic() < stop:
        time.sleep(0.005)
    if not (root / 'continue').is_file():
        raise RuntimeError('fixture gate expired')
    return result

with patch.object(bundle, 'inspect', verify):
    try:
        bundle.build(root / 'result.bundle', 'a' * 40, pins,
                     {'bin/demo': source, 'notice': source},
                     dependencies={'fixture': 'sha256:' + 'b' * 64},
                     toolchains={'fixture': 'sha256:' + 'c' * 64}, licenses={'notice': bundle.file_sha(source)},
                     entrypoints={'demo':'bin/demo'}, executable=('bin/demo',))
    except ValueError as error:
        if str(error) != 'bundle destination already exists':
            raise
        print('lost')
    else:
        print('published')
"""


class AtomicBundlePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        own = tempfile.TemporaryDirectory()
        self.addCleanup(own.cleanup)
        self.root = Path(own.name)
        self.source = self.root / "source"
        self.source.write_bytes(b"synthetic executable bytes, never executed")

    def build(self, target: Path) -> tuple[Any, str]:
        manifest, digest = bundle.build(
            target,
            "a" * 40,
            PINS,
            {"bin/demo": self.source, "notice": self.source},
            dependencies={"fixture": "sha256:" + "b" * 64},
            toolchains={"fixture": "sha256:" + "c" * 64},
            licenses={"notice": bundle.file_sha(self.source)},
            entrypoints={"demo": "bin/demo"},
            executable=("bin/demo",),
        )
        return manifest, digest

    def test_two_verified_fresh_processes_preserve_the_single_published_winner(
        self,
    ) -> None:
        names = ("first", "second")
        workers = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", BUILD_WORKER, str(self.root), name],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for name in names
        ]
        live = self.root / "result.bundle"
        try:
            deadline = time.monotonic() + 6
            while not all((self.root / (name + ".ready")).is_file() for name in names):
                self.assertFalse(live.exists(), "unverified publication was visible")
                if not all(worker.poll() is None for worker in workers):
                    self.fail("builder exited before completing verification")
                if time.monotonic() >= deadline:
                    self.fail(
                        "both builders did not reach the private verification gate"
                    )
                time.sleep(0.005)
            self.assertFalse(live.exists(), "bundle appeared before both were released")
            (self.root / "continue").touch()
            results = []
            for worker in workers:
                stdout, stderr = worker.communicate(timeout=6)
                self.assertEqual(worker.returncode, 0, stderr)
                results.append(stdout.strip())
            self.assertCountEqual(results, ["published", "lost"])
            winner = names[results.index("published")]
            manifest, _ = bundle.inspect(live, component_commits=PINS)
            self.assertEqual(
                manifest["files"]["bin/demo"]["sha256"], bundle.sha(winner.encode())
            )
            self.assertEqual(list(self.root.glob(".runspindle-bundle-*")), [])
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                worker.communicate(timeout=5)

    def test_verification_failure_never_publishes_or_leaks_private_staging(
        self,
    ) -> None:
        destination = self.root / "output.bundle"
        with patch.object(
            bundle, "inspect", side_effect=ValueError("fixture failed verification")
        ):
            with self.assertRaisesRegex(ValueError, "fixture failed verification"):
                self.build(destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".runspindle-bundle-*")), [])

    def test_existing_destination_symlink_is_preserved(self) -> None:
        destination = self.root / "output.bundle"
        missing = self.root / "missing"
        destination.symlink_to(missing)
        with self.assertRaisesRegex(ValueError, "destination already exists"):
            self.build(destination)
        self.assertTrue(destination.is_symlink())
        self.assertFalse(missing.exists())

    def test_archive_stage_recovers_from_partial_copy_and_preserves_verified_bytes(
        self,
    ) -> None:
        source = self.root / "source.bundle"
        manifest, digest = self.build(source)
        archives = self.root / "archives"
        archives.mkdir()
        partial = archives / (digest + ".next")
        partial.write_bytes(b"simulated interrupted copy")
        bundle._retain_archive(
            source,
            archives,
            manifest,
            digest,
            component_commits=PINS,
            target="linux/arm64",
        )
        saved = archives / digest
        self.assertEqual(saved.read_bytes(), source.read_bytes())
        self.assertFalse(partial.exists())
        previous = saved.stat().st_ino
        bundle._retain_archive(
            source,
            archives,
            manifest,
            digest,
            component_commits=PINS,
            target="linux/arm64",
        )
        self.assertEqual(saved.stat().st_ino, previous)

    def test_existing_archive_corruption_remains_available_for_investigation(
        self,
    ) -> None:
        source = self.root / "source.bundle"
        manifest, digest = self.build(source)
        archives = self.root / "archives"
        archives.mkdir()
        saved = archives / digest
        damage = source.read_bytes()
        damage = damage[:-1] + bytes([damage[-1] ^ 1])
        saved.write_bytes(damage)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            bundle._retain_archive(
                source,
                archives,
                manifest,
                digest,
                component_commits=PINS,
                target="linux/arm64",
            )
        self.assertEqual(saved.read_bytes(), damage)
        self.assertEqual(list(archives.iterdir()), [saved])

    def test_failed_archive_copy_clears_its_stage_for_retry(self) -> None:
        source = self.root / "source.bundle"
        manifest, digest = self.build(source)
        archives = self.root / "archives"
        archives.mkdir()
        sender = source.read_bytes()
        source.write_bytes(sender[:-1] + bytes([sender[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            bundle._retain_archive(
                source,
                archives,
                manifest,
                digest,
                component_commits=PINS,
                target="linux/arm64",
            )
        self.assertEqual(list(archives.iterdir()), [])
        source.write_bytes(sender)
        bundle._retain_archive(
            source,
            archives,
            manifest,
            digest,
            component_commits=PINS,
            target="linux/arm64",
        )
        self.assertEqual((archives / digest).read_bytes(), sender)

    def test_archive_stage_rejects_special_files_and_hard_links_without_opening(
        self,
    ) -> None:
        source = self.root / "source.bundle"
        manifest, digest = self.build(source)
        archives = self.root / "archives"
        archives.mkdir()
        partial = archives / (digest + ".next")
        with patch.object(
            Path,
            "open",
            side_effect=AssertionError("stage opened before path admission"),
        ):
            os.mkfifo(partial)
            with self.assertRaisesRegex(ValueError, "unsafe retained archive path"):
                bundle._retain_archive(
                    source,
                    archives,
                    manifest,
                    digest,
                    component_commits=PINS,
                    target="linux/arm64",
                )
            self.assertTrue(stat.S_ISFIFO(partial.lstat().st_mode))
            partial.unlink()
            os.link(self.source, partial)
            with self.assertRaisesRegex(ValueError, "unsafe retained archive path"):
                bundle._retain_archive(
                    source,
                    archives,
                    manifest,
                    digest,
                    component_commits=PINS,
                    target="linux/arm64",
                )
            self.assertEqual(partial.lstat().st_ino, self.source.lstat().st_ino)
