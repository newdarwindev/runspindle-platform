"""Real scratch files/SQLite; synthetic target admission, not ARM64 qualification."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from tools import development_bundle as bundle_module

# Existing bundle APIs use dynamic JSON contracts.
b: Any = bundle_module

PINS = {name: b.sha(name.encode())[:40] for name in b.COMPONENTS}


def bundle(label: str) -> bytes:
    files = {"payload.txt": label.encode(), "notice.txt": b"Synthetic original"}
    return cast(
        bytes,
        b.build(
            b.VERSION,
            "a" * 40,
            PINS,
            files,
            dependencies={"test": "sha256:" + "b" * 64},
            toolchains={"test": "sha256:" + "c" * 64},
            licenses={"notice.txt": b.sha(files["notice.txt"])},
        ),
    )


class MetadataPointerTests(unittest.TestCase):
    def setUp(self) -> None:
        # Only architecture admission is synthetic. Journal, files, hashes,
        # installation/recovery and pointer operations are the actual code.
        admission = patch.object(b, "actual_target", return_value="linux/arm64")
        admission.start()
        self.addCleanup(admission.stop)
        self.target = b.DisposableTarget.create()
        self.addCleanup(self.target.cleanup)
        self.ids: list[str] = []
        for generation, label in enumerate(("first", "second", "third")):
            receipt = self.target.install(
                b.VERSION,
                bundle(label),
                component_commits=PINS,
                expected_generation=generation,
            )
            self.ids.append(receipt["manifest_sha256"])

    def point(self, digest: str) -> None:
        (self.target.root / "active.json").write_bytes(
            b.canonical({"manifest_sha256": digest})
        )

    def snapshot(self) -> tuple[Any, Any]:
        return (
            list(self.target.db.iterdump()),
            {
                str(p.relative_to(self.target.root)): p.read_bytes()
                for p in self.target.root.rglob("*")
                if p.is_file() and not p.name.startswith("journal.sqlite")
            },
        )

    def test_historical_pointer_rejected_without_recovery_effects(self) -> None:
        self.point(self.ids[0])
        before = self.snapshot()
        checkpoint = Mock()
        with self.assertRaisesRegex(ValueError, "conflicting installation readback"):
            self.target.recover(
                b.VERSION, component_commits=PINS, checkpoint=checkpoint
            )
        checkpoint.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_unrelated_retained_pointer_rejected_during_pending(self) -> None:
        self.target.db.execute("UPDATE state SET pending=?", (self.ids[1],))
        self.point(self.ids[0])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "conflicting installation readback"):
            self.target.verify_installed(PINS)
        self.assertEqual(self.snapshot(), before)

    def test_active_and_pending_pointers_remain_valid(self) -> None:
        self.target.db.execute("UPDATE state SET pending=?", (self.ids[1],))
        for digest in (self.ids[2], self.ids[1]):
            self.point(digest)
            self.assertEqual(
                self.target.verify_installed(PINS)["manifest_sha256"], digest
            )

    def test_null_and_unrecognized_pointers_rejected(self) -> None:
        self.target.db.execute("UPDATE state SET active=NULL,pending=NULL")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "conflicting installation readback"):
            self.target.verify_installed(PINS)
        self.assertEqual(self.snapshot(), before)
        (self.target.root / "active.json").unlink()
        with self.assertRaisesRegex(ValueError, "conflicting installation readback"):
            self.target.verify_installed(PINS)
        self.point("f" * 64)
        with self.assertRaisesRegex(ValueError, "conflicting installation readback"):
            self.target.verify_installed(PINS)

    def test_interrupted_install_preserves_valid_pending_transitions(self) -> None:
        for stage in ("after_install_intent", "after_installed_pointer"):

            def interrupt(point: str) -> None:
                if point == stage:
                    raise RuntimeError("synthetic interruption")

            content = bundle(stage)
            expected = b.verify(content, component_commits=PINS)[2]
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                self.target.install(
                    b.VERSION,
                    content,
                    component_commits=PINS,
                    expected_generation=self.target.state()["generation"],
                    checkpoint=interrupt,
                )
            observed = json.loads((self.target.root / "active.json").read_bytes())[
                "manifest_sha256"
            ]
            self.assertEqual(
                self.target.verify_installed(PINS)["manifest_sha256"], observed
            )
            self.assertEqual(
                self.target.recover(b.VERSION, component_commits=PINS)[
                    "manifest_sha256"
                ],
                expected,
            )

    def interrupt_rollback(self) -> None:
        with patch.object(
            self.target, "recover", side_effect=RuntimeError("after intent")
        ):
            with self.assertRaisesRegex(RuntimeError, "after intent"):
                self.target.rollback(
                    b.VERSION, component_commits=PINS, expected_generation=3
                )

    def test_rollback_predecessor_valid_after_recorded_intent(self) -> None:
        self.interrupt_rollback()
        path = self.target.root / "versions" / self.ids[1] / "payload.txt"
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        previous_mtime = path.stat().st_mtime_ns
        self.assertEqual(self.target.state()["pending"], self.ids[1])
        self.assertEqual(
            self.target.verify_installed(PINS)["manifest_sha256"], self.ids[2]
        )
        self.assertEqual(
            self.target.recover(b.VERSION, component_commits=PINS)["manifest_sha256"],
            self.ids[1],
        )
        self.assertEqual(path.stat().st_mtime_ns, previous_mtime)

    def test_interrupted_rollback_does_not_recreate_verified_predecessor(self) -> None:
        self.interrupt_rollback()
        predecessor = self.target.root / "versions" / self.ids[1] / "payload.txt"
        predecessor.unlink()
        before = self.snapshot()
        probe = """
import sys
from contextlib import closing
from unittest.mock import patch

from tools import development_bundle as bundle
from test_metadata_pointer import PINS

with patch.object(bundle, "actual_target", return_value="linux/arm64"):
    with closing(bundle.DisposableTarget(sys.argv[1])) as target:
        try:
            target.recover(bundle.VERSION, component_commits=PINS)
        except ValueError as error:
            if str(error) != "installed byte mismatch":
                raise
            sys.exit(73)
"""
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [
                sys.executable,
                *(["-" + "O" * sys.flags.optimize] if sys.flags.optimize else []),
                "-B",
                "-c",
                probe,
                str(self.target.root),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "PYTHONPATH": os.pathsep.join((".", "tests"))},
        )
        self.assertEqual(process.returncode, 73, process.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(predecessor.exists())
        self.assertEqual(self.target.state()["active"], self.ids[2])
        self.assertEqual(self.target.state()["pending"], self.ids[1])

    def test_wrong_archive_identity_cannot_change_the_pointer(self) -> None:
        self.interrupt_rollback()
        db = self.target.db
        trigger = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='bundle_immutable'"
        ).fetchone()[0]
        db.execute("DROP TRIGGER bundle_immutable")
        db.execute(
            "UPDATE installs SET bundle=? WHERE digest=?",
            (bundle("first"), self.ids[1]),
        )
        db.execute(trigger)
        before = self.snapshot()
        checkpoint = Mock()
        with self.assertRaisesRegex(ValueError, "pending manifest identity mismatch"):
            self.target.recover(
                b.VERSION, component_commits=PINS, checkpoint=checkpoint
            )
        checkpoint.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_symlinked_predecessor_rejected_before_rollback_intent(self) -> None:
        versions = self.target.root / "versions"
        predecessor = versions / self.ids[1]
        moved = versions / "retained-synthetic-files"
        predecessor.rename(moved)
        predecessor.symlink_to(moved, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "rollback bytes unavailable"):
            self.target.rollback(
                b.VERSION, component_commits=PINS, expected_generation=3
            )
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
