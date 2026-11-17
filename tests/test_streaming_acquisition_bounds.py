"""Portable byte-acquisition evidence, not native installed-service acceptance."""

import hashlib
import io
import os
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from unittest.mock import patch

from tools import streaming_bundle as bundle_module
from tools.development_bundle import COMPONENTS

bundle: Any = bundle_module


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PINS = {name: sha(name.encode())[:40] for name in COMPONENTS}


class StreamingAcquisitionBounds(unittest.TestCase):
    def test_exact_copy_never_writes_growth_and_rejects_truncation(self) -> None:
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "grew"):
            bundle._copy_exact(io.BytesIO(b"abc-overrun"), output, 3)
        self.assertEqual(output.getvalue(), b"abc")
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "shrank"):
            bundle._copy_exact(io.BytesIO(b"ab"), output, 3)
        self.assertEqual(output.getvalue(), b"ab")
        output = io.BytesIO()
        bundle._copy_exact(io.BytesIO(b""), output, 0)
        self.assertEqual(output.getvalue(), b"")

    def test_nonregular_inputs_reject_in_bounded_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            regular = root / "regular"
            regular.write_bytes(b"content")
            link = root / "link"
            link.symlink_to(regular)
            for path in (fifo, link, root):
                with self.subTest(path=path.name):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "from tools.streaming_bundle import file_sha;import sys;file_sha(sys.argv[1])",
                            str(path),
                        ],
                        capture_output=True,
                        timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
            self.assertEqual(regular.read_bytes(), b"content")

    def test_oversize_fails_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            path.write_bytes(b"12345")
            with patch.object(bundle, "MAX_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    bundle.file_sha(path)

    def test_build_changed_file_is_rejected_and_incomplete_output_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"#!/bin/sh\nexit 0\n")
            notice = root / "notice"
            notice.write_bytes(b"Synthetic original fixture")
            destination = root / "bundle"
            original = bundle._regular_source
            calls = 0

            @contextmanager
            def changed(path: Path | str, limit: int) -> Iterator[tuple[BinaryIO, int]]:
                nonlocal calls
                if Path(path) == source:
                    calls += 1
                    if calls == 2:  # actual acquisition after manifest hashing
                        with source.open("ab") as output:
                            output.write(b"unexpected growth")
                with original(path, limit) as opened:
                    yield opened

            with patch.object(bundle, "_regular_source", changed):
                with self.assertRaisesRegex(ValueError, "source size changed"):
                    bundle.build(
                        destination,
                        "a" * 40,
                        PINS,
                        {"bin/task": source, "notice": notice},
                        dependencies={"fixture": "sha256:" + sha(b"dependency")},
                        toolchains={"fixture": "sha256:" + sha(b"toolchain")},
                        licenses={"notice": sha(notice.read_bytes())},
                        entrypoints={"task": "bin/task"},
                        executable=["bin/task"],
                    )
            self.assertFalse(destination.exists())
            self.assertEqual(notice.read_bytes(), b"Synthetic original fixture")

    def test_installed_special_nodes_are_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.write_bytes(b"fixture")
            payload.chmod(0o644)
            manifest = {
                "files": {
                    "payload": {"size": 7, "sha256": sha(b"fixture"), "mode": 0o644}
                }
            }
            bundle.verify_tree(root, manifest)
            fifo = root / "extra"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "special file"):
                bundle.verify_tree(root, manifest)
            fifo.unlink()
            bundle.verify_tree(root, manifest)

    def test_deliberate_growth_guard_removal_is_observed(self) -> None:
        source = Path(bundle.__file__).read_text()
        needle = '_check(not source.read(1), "source grew during acquisition")'
        self.assertEqual(source.count(needle), 1)
        mutant: Any = types.ModuleType("streaming_growth_mutant")
        exec(source.replace(needle, "pass"), mutant.__dict__)
        output = io.BytesIO()
        # Original fails on this same input. Mutation silently accepts growth.
        mutant._copy_exact(io.BytesIO(b"abc-overrun"), output, 3)
        self.assertEqual(output.getvalue(), b"abc")

    def test_build_file_count_limit_precedes_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(bundle, "MAX_FILES", 1),
                patch.object(bundle, "file_sha") as hashed,
            ):
                with self.assertRaisesRegex(ValueError, "file limit"):
                    bundle.build(
                        root / "output",
                        "a" * 40,
                        PINS,
                        {"one": root / "one", "two": root / "two"},
                        dependencies={},
                        toolchains={},
                        licenses={},
                        entrypoints={},
                    )
                hashed.assert_not_called()
            self.assertFalse((root / "output").exists())

    def test_socket_node_rejection_when_local_socket_capability_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                server = socket.socket(socket.AF_UNIX)
            except PermissionError:
                self.skipTest(
                    "local Unix socket capability denied; FIFO tested separately"
                )
            with server:
                server.bind(str(root / "extra"))
                with self.assertRaisesRegex(ValueError, "special file"):
                    bundle.verify_tree(root, {"files": {}})


if __name__ == "__main__":
    unittest.main()
