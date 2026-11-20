import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import streaming_bundle as b
from tools.development_bundle import COMPONENTS, actual_target, sha

PINS = {name: sha(name.encode())[:40] for name in COMPONENTS}


SMOKE = "bin/libexec/smoke"


def build_bundle(root, label):
    source = root / (label + ".source")
    source.write_bytes(label.encode())
    notice = root / "notice"
    notice.write_bytes(b"Synthetic test rights retained")
    archive = root / (label + ".bundle")
    manifest, digest = b.build(
        archive,
        "a" * 40,
        PINS,
        {SMOKE: source, "notices/test": notice},
        dependencies={"test": "sha256:" + sha(b"dependency")},
        toolchains={"test": "sha256:" + sha(b"toolchain")},
        licenses={"notices/test": b.file_sha(notice)},
        entrypoints={"smoke": SMOKE},
        executable=[SMOKE],
    )
    return archive, manifest, digest


class ExtractionSyncTests(unittest.TestCase):
    def test_extraction_syncs_final_modes_then_directories_bottom_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, _, _ = build_bundle(root, "first")
            destination = root / "extract"
            destination.mkdir()
            calls = []
            fsync = os.fsync

            def observe(descriptor):
                info = os.fstat(descriptor)
                calls.append((info.st_ino, stat.S_IMODE(info.st_mode)))
                fsync(descriptor)

            with patch.object(b.os, "fsync", side_effect=observe):
                b.inspect(archive, component_commits=PINS, destination=destination)
            synced = {
                inode: (position, mode) for position, (inode, mode) in enumerate(calls)
            }
            for name, mode in [(SMOKE, 0o755), ("notices/test", 0o644)]:
                file = destination / name
                position, observed_mode = synced[file.stat().st_ino]
                self.assertEqual(observed_mode, mode)
                for parent in file.parents:
                    parent_position, _ = synced[parent.stat().st_ino]
                    self.assertLess(position, parent_position)
                    position = parent_position
                    if parent == destination:
                        break


@unittest.skipUnless(
    actual_target() == "linux/arm64", "disposable Linux arm64 required"
)
class InstalledStageSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = b.StreamingTarget.create()
        self.addCleanup(self.target.cleanup)

    def invoke(self, code, *args, expected=0):
        script = (
            "import os,sys,json; sys.path.insert(0,'tests'); from test_streaming_stage_sync import PINS; from tools.streaming_bundle import StreamingTarget,VERSION; t=StreamingTarget(sys.argv[1]); "
            + code
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.target.root), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, expected, completed.stderr)
        return completed

    def test_directory_sync_failure_preserves_active_and_fresh_process_recovers(self):
        first, _, first_digest = build_bundle(Path(self.tmp.name), "first")
        self.target.install(
            b.VERSION, first, component_commits=PINS, expected_generation=0
        )
        second, manifest, second_digest = build_bundle(Path(self.tmp.name), "second")
        result = self.invoke(
            """
from tools import streaming_bundle
sync_dir = streaming_bundle._sync_dir
def interrupted(path):
    if path.name == "libexec":
        raise OSError("synthetic directory sync failure")
    sync_dir(path)
streaming_bundle._sync_dir = interrupted
t.install(VERSION, sys.argv[2], component_commits=PINS, expected_generation=1)
""",
            second,
            expected=1,
        )
        self.assertIn("synthetic directory sync failure", result.stderr)
        pointer = self.target.root / "active.json"
        self.assertEqual(
            json.loads(pointer.read_bytes())["manifest_sha256"], first_digest
        )
        version = self.target.root / "versions" / second_digest
        self.assertFalse(version.exists())
        with closing(sqlite3.connect(self.target.root / "journal.sqlite")) as journal:
            self.assertEqual(
                journal.execute(
                    "SELECT generation,active,pending FROM state"
                ).fetchone(),
                (2, first_digest, second_digest),
            )
        self.invoke("t.recover(VERSION, component_commits=PINS)")
        self.assertEqual(
            json.loads(pointer.read_bytes())["manifest_sha256"], second_digest
        )
        with closing(sqlite3.connect(self.target.root / "journal.sqlite")) as journal:
            self.assertEqual(
                journal.execute(
                    "SELECT generation,active,pending FROM state"
                ).fetchone(),
                (2, second_digest, None),
            )
        smoke = version / SMOKE
        self.assertEqual(sha(smoke.read_bytes()), manifest["files"][SMOKE]["sha256"])
        self.assertEqual(stat.S_IMODE(smoke.stat().st_mode), 0o755)
