import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from tools import streaming_bundle as b
from tools.development_bundle import COMPONENTS, actual_target, canonical, sha

PINS = {name: sha(name.encode())[:40] for name in COMPONENTS}


def make(root, label="first"):
    root = Path(root)
    source = root / (label + ".source")
    source.write_bytes(
        b'#!/bin/sh\nprintf "' + label.encode() + b'"\n' + b"#" * (2 * 1024 * 1024)
    )
    notice = root / "notice"
    notice.write_bytes(b"Synthetic test rights retained")
    archive = root / (label + ".bundle")
    manifest, digest = b.build(
        archive,
        "a" * 40,
        PINS,
        {"bin/smoke": source, "notices/test": notice},
        dependencies={"test": "sha256:" + sha(b"dependency")},
        toolchains={"test": "sha256:" + sha(b"toolchain")},
        licenses={"notices/test": b.file_sha(notice)},
        entrypoints={"smoke": "bin/smoke"},
        executable=["bin/smoke"],
    )
    return archive, manifest, digest


class StreamingTests(unittest.TestCase):
    def test_large_file_extract_and_integrity_negative_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, manifest, digest = make(root)
            self.assertGreater(manifest["files"]["bin/smoke"]["size"], 1048576)
            destination = root / "extract"
            destination.mkdir()
            self.assertEqual(
                b.inspect(archive, component_commits=PINS, destination=destination),
                (manifest, digest),
            )
            b.verify_tree(destination, manifest)
            self.assertEqual((destination / "bin/smoke").stat().st_mode & 0o777, 0o755)
            with self.assertRaises(ValueError):
                b.inspect(archive, component_commits={**PINS, "core": "b" * 40})
            with self.assertRaises(ValueError):
                b.inspect(archive, component_commits=PINS, target="darwin/arm64")
            raw = archive.read_bytes()
            for changed in [
                raw[:-1],
                raw + b"extra",
                raw[:-1] + b"x",
                b"future" + raw[6:],
            ]:
                bad = root / "bad"
                bad.write_bytes(changed)
                with self.assertRaises(ValueError):
                    b.inspect(bad, component_commits=PINS)

    def test_manifest_paths_links_modes_versions_and_rights_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, manifest, _ = make(root)
            for name in ("../outside", "/absolute", "bin/../outside", "bin//smoke"):
                changed = json.loads(json.dumps(manifest))
                changed["files"][name] = changed["files"].pop("bin/smoke")
                with self.assertRaises(ValueError):
                    b.validate_manifest(changed, component_commits=PINS)
            for field, value in [
                ("version", "future"),
                ("licenses", {}),
                ("entrypoints", {}),
                ("dependencies", {"floating": "latest"}),
            ]:
                changed = {**manifest, field: value}
                with self.assertRaises(ValueError):
                    b.validate_manifest(changed, component_commits=PINS)
            changed = json.loads(json.dumps(manifest))
            changed["files"]["bin/smoke"]["mode"] = 0o4755
            with self.assertRaises(ValueError):
                b.validate_manifest(changed, component_commits=PINS)
            link = root / "link"
            link.symlink_to(root / "notice")
            with self.assertRaises(ValueError):
                b.build(
                    root / "link.bundle",
                    "a" * 40,
                    PINS,
                    {"notices/link": link},
                    dependencies={},
                    toolchains={},
                    licenses={},
                    entrypoints={},
                )
            header = b'{"version":1,"version":2}'
            (root / "duplicate").write_bytes(
                b.MAGIC + struct.pack(">Q", len(header)) + header
            )
            with self.assertRaises(ValueError):
                b.inspect(root / "duplicate", component_commits=PINS)


@unittest.skipUnless(
    actual_target() == "linux/arm64", "disposable Linux arm64 required"
)
class InstalledStreamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = b.StreamingTarget.create()
        self.addCleanup(self.target.cleanup)

    def invoke(self, code, *args, expected=0):
        script = (
            "import os,sys,json; sys.path.insert(0,'tests'); from test_streaming_bundle import PINS; from tools.streaming_bundle import StreamingTarget,VERSION; t=StreamingTarget(sys.argv[1]); "
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

    def test_fresh_process_upgrade_rollback_and_independent_execution(self):
        first, _, first_digest = make(self.tmp.name)
        self.target.install(
            b.VERSION, first, component_commits=PINS, expected_generation=0
        )
        for point in (
            "after_install_intent",
            "after_staged_payload",
            "after_installed_pointer",
        ):
            archive, _, digest = make(self.tmp.name, point)
            generation = self.target.state()["generation"]
            previous = self.target.state()["active"]
            self.invoke(
                "t.install(VERSION,sys.argv[2],component_commits=PINS,expected_generation=int(sys.argv[3]),checkpoint=lambda p:os._exit(101) if p==sys.argv[4] else None)",
                archive,
                generation,
                point,
                expected=101,
            )
            pointer = json.loads((self.target.root / "active.json").read_bytes())[
                "manifest_sha256"
            ]
            self.assertEqual(
                pointer, digest if point == "after_installed_pointer" else previous
            )
            self.invoke("t.recover(VERSION,component_commits=PINS);t.close()")
            active = self.target.root / "versions" / digest / "bin/smoke"
            self.assertEqual(subprocess.check_output([active], text=True), point)
            self.assertEqual(
                b.file_sha(active),
                b.inspect(archive, component_commits=PINS)[0]["files"]["bin/smoke"][
                    "sha256"
                ],
            )
        generation = self.target.state()["generation"]
        previous = self.target.db.execute(
            "SELECT predecessor FROM installs WHERE digest=?", (digest,)
        ).fetchone()[0]
        self.invoke(
            "t.rollback(VERSION,component_commits=PINS,expected_generation=int(sys.argv[2]),checkpoint=lambda p:os._exit(102) if p=='after_rollback_intent' else None)",
            generation,
            expected=102,
        )
        self.invoke("t.recover(VERSION,component_commits=PINS);t.close()")
        self.assertEqual(self.target.state()["active"], previous)
        with self.assertRaises(ValueError):
            self.target.rollback(
                b.VERSION, component_commits=PINS, expected_generation=generation
            )

    def test_conflicting_readback_unavailable_predecessor_and_failed_rollback(self):
        first, _, old = make(self.tmp.name)
        self.target.install(
            b.VERSION, first, component_commits=PINS, expected_generation=0
        )
        second, _, current = make(self.tmp.name, "second")
        self.target.install(
            b.VERSION, second, component_commits=PINS, expected_generation=1
        )
        pointer = self.target.root / "active.json"
        original = pointer.read_bytes()
        pointer.write_bytes(canonical({"manifest_sha256": "f" * 64}))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.target.recover(b.VERSION, component_commits=PINS)
        pointer.write_bytes(original)
        (self.target.root / "versions" / old / "bin/smoke").unlink()
        with self.assertRaises(ValueError):
            self.target.rollback(
                b.VERSION, component_commits=PINS, expected_generation=2
            )
        self.assertEqual(
            self.target.state(), {"generation": 2, "active": current, "pending": None}
        )
        self.assertEqual(pointer.read_bytes(), original)
        (self.target.root / "archives" / old).unlink()
        with self.assertRaises(ValueError):
            self.target.rollback(
                b.VERSION, component_commits=PINS, expected_generation=2
            )

    def test_installed_corruption_and_unsafe_links(self):
        archive, _, digest = make(self.tmp.name)
        self.target.install(
            b.VERSION, archive, component_commits=PINS, expected_generation=0
        )
        path = self.target.root / "versions" / digest / "bin/smoke"
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "installed byte or mode mismatch"):
            self.target.verify_installed(PINS)
        path.chmod(0o755)
        path.unlink()
        path.symlink_to(archive)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.target.verify_installed(PINS)
        path.unlink()

    def test_metadata_and_stream_journals_cannot_cross_payload_kinds(self):
        from tools.development_bundle import DisposableTarget, VERSION as OLD_VERSION
        from test_development_bundle import bundle as metadata_bundle, PINS as OLD_PINS

        # Both objects can be opened on an empty journal. A later competing
        # installation must still fence the other payload family before intent.
        metadata_target = DisposableTarget(self.target.root)
        try:
            archive, _, _ = make(self.tmp.name)
            self.target.install(
                b.VERSION, archive, component_commits=PINS, expected_generation=0
            )
            prior = self.target.state()
            with self.assertRaisesRegex(ValueError, "journal payload kind"):
                metadata_target.install(
                    OLD_VERSION,
                    metadata_bundle(),
                    component_commits=OLD_PINS,
                    expected_generation=1,
                )
            self.assertEqual(self.target.state(), prior)
            with self.assertRaisesRegex(ValueError, "journal payload kind"):
                DisposableTarget(self.target.root)
        finally:
            metadata_target.close()
        metadata_target = DisposableTarget.create()
        streaming_target = b.StreamingTarget(metadata_target.root)
        try:
            metadata_target.install(
                OLD_VERSION,
                metadata_bundle(),
                component_commits=OLD_PINS,
                expected_generation=0,
            )
            prior = metadata_target.state()
            with self.assertRaisesRegex(ValueError, "journal payload kind"):
                streaming_target.install(
                    b.VERSION, archive, component_commits=PINS, expected_generation=1
                )
            self.assertEqual(metadata_target.state(), prior)
            with self.assertRaisesRegex(ValueError, "journal payload kind"):
                b.StreamingTarget(metadata_target.root)
        finally:
            streaming_target.close()
            metadata_target.cleanup()

    def test_concurrent_ownership_and_stale_generation(self):
        first, _, digest = make(self.tmp.name)
        script = "import sys;sys.path.insert(0,'tests');from test_streaming_bundle import PINS;from tools.streaming_bundle import StreamingTarget,VERSION;t=StreamingTarget(sys.argv[1]);t.install(VERSION,sys.argv[2],component_commits=PINS,expected_generation=0);t.close()"
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.target.root), str(first)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [p.communicate(timeout=30) for p in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1], results)
        self.assertEqual(self.target.verify_installed(PINS)["manifest_sha256"], digest)


if __name__ == "__main__":
    unittest.main()
