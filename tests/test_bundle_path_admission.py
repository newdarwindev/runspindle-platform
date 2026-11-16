"""Malformed bundle admission: real scratch bytes/SQLite, no target activation."""

import base64
import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools import development_bundle as metadata_module
from tools import streaming_bundle as streaming_module

# Existing JSON bundle APIs are dynamic; no new runtime contract is implied.
d: Any = metadata_module
s: Any = streaming_module
PINS = {name: "a" * 40 for name in d.COMPONENTS}
INVENTORY = {"synthetic": "sha256:" + "b" * 64}


def metadata_bytes(files: dict[str, bytes]) -> bytes:
    """External producer: rehash malformed records without calling build()."""
    valid = d.build(
        d.VERSION,
        "c" * 40,
        PINS,
        {"notice": b"original"},
        dependencies=INVENTORY,
        toolchains=INVENTORY,
        licenses={"notice": d.sha(b"original")},
    )
    value = json.loads(valid)
    value["manifest"]["files"] = {name: d.sha(data) for name, data in files.items()}
    value["manifest"]["licenses"] = dict(value["manifest"]["files"])
    value["manifest_sha256"] = d.sha(d.canonical(value["manifest"]))
    value["files"] = {
        name: base64.b64encode(data).decode() for name, data in files.items()
    }
    return bytes(d.canonical(value))


def stream_bytes(files: dict[str, bytes]) -> bytes:
    manifest = dict(
        version=s.VERSION,
        kind="development_only",
        target="linux/arm64",
        composition_commit="c" * 40,
        component_commits=PINS,
        dependencies=INVENTORY,
        toolchains=INVENTORY,
        licenses={name: d.sha(data) for name, data in files.items()},
        files={
            name: {"size": len(data), "sha256": d.sha(data), "mode": 0o755}
            for name, data in files.items()
        },
        entrypoints={"synthetic": next(iter(files))},
        unsupported=d.UNSUPPORTED,
    )
    header = d.canonical(manifest)
    return bytes(
        s.MAGIC
        + struct.pack(">Q", len(header))
        + header
        + b"".join(files[name] for name in sorted(files))
    )


class BundlePathAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="bundle-path-admission-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = sqlite3.connect(self.root / "synthetic.sqlite", isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(
            "CREATE TABLE state(id,generation,active,pending);"
            "INSERT INTO state VALUES(1,0,NULL,NULL);"
            "CREATE TABLE installs(digest,bundle,predecessor,state);"
        )
        (self.root / "active.json").write_bytes(b"synthetic unchanged pointer")

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def rejected_install(self, api: Any, payload: Any) -> None:
        # Bypass native constructor entirely. Only admission is under test;
        # actual_target is synthetic, never proof of ARM64 compatibility.
        cls = api.DisposableTarget if api is d else api.StreamingTarget
        target = object.__new__(cls)
        target.root = self.root
        target.db = self.db
        target.recover = Mock(side_effect=AssertionError("recovery forbidden"))
        checkpoint = Mock()
        statements: list[str] = []
        self.db.set_trace_callback(statements.append)
        before = self.snapshot()
        with patch.object(api, "actual_target", return_value="linux/arm64"):
            with self.assertRaises(ValueError):
                target.install(
                    api.VERSION,
                    payload,
                    component_commits=PINS,
                    expected_generation=0,
                    checkpoint=checkpoint,
                )
        self.assertEqual(statements, [])
        self.assertEqual(self.snapshot(), before)
        checkpoint.assert_not_called()
        target.recover.assert_not_called()
        self.db.set_trace_callback(None)

    def test_metadata_dot_rejected_before_any_install_effect(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe bundle path"):
            d.build(
                d.VERSION,
                "c" * 40,
                PINS,
                {".": b"original"},
                dependencies=INVENTORY,
                toolchains=INVENTORY,
                licenses={".": d.sha(b"original")},
            )
        self.rejected_install(d, metadata_bytes({".": b"root is not a file"}))

    def test_metadata_ancestor_conflicts_reject_independent_of_order(self) -> None:
        for names in (("a", "a/b"), ("a/b/c", "a"), ("a/b", "a/b/c")):
            for ordered in (names, tuple(reversed(names))):
                with self.subTest(names=ordered):
                    files = {name: name.encode() for name in ordered}
                    with self.assertRaisesRegex(
                        ValueError, "file/directory path conflict"
                    ):
                        d.build(
                            d.VERSION,
                            "c" * 40,
                            PINS,
                            files,
                            dependencies=INVENTORY,
                            toolchains=INVENTORY,
                            licenses={
                                name: d.sha(data) for name, data in files.items()
                            },
                        )
                    self.rejected_install(d, metadata_bytes(files))

    def test_stream_root_dot_record_and_entrypoint_reject_before_effect(self) -> None:
        archive = self.root / "malformed.bundle"
        archive.write_bytes(stream_bytes({".": b"not executable"}))
        self.rejected_install(s, archive)
        empty = self.root / "extract"
        empty.mkdir()
        with self.assertRaises(ValueError):
            s.inspect(archive, component_commits=PINS, destination=empty)
        self.assertEqual(list(empty.iterdir()), [])

    def test_stream_ancestor_conflict_remains_rejected(self) -> None:
        archive = self.root / "malformed.bundle"
        archive.write_bytes(stream_bytes({"a/b": b"nested", "a": b"parent"}))
        self.rejected_install(s, archive)

    def test_valid_nested_metadata_roundtrip(self) -> None:
        files = {"a/b": b"one", "a/c/d": b"two", "notice": b"original"}
        data = d.build(
            d.VERSION,
            "c" * 40,
            PINS,
            files,
            dependencies=INVENTORY,
            toolchains=INVENTORY,
            licenses={"notice": d.sha(files["notice"])},
        )
        self.assertEqual(d.verify(data, component_commits=PINS)[1], files)

    def test_valid_stream_wheel_punctuation_and_nested_siblings(self) -> None:
        files = {
            "lib/.wheel-1.0+cpu.dist-info/METADATA": b"original",
            "lib/pkg/__init__.py": b"# inert",
            "bin/smoke": b"inert text",
        }
        archive = self.root / "valid.bundle"
        archive.write_bytes(stream_bytes(files))
        destination = self.root / "extract"
        destination.mkdir()
        manifest, _ = s.inspect(
            archive, component_commits=PINS, destination=destination
        )
        s.verify_tree(destination, manifest)
        self.assertEqual(
            {
                str(p.relative_to(destination)): p.read_bytes()
                for p in destination.rglob("*")
                if p.is_file()
            },
            files,
        )


if __name__ == "__main__":
    unittest.main()
