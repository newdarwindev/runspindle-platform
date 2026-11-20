"""Real scratch files/SQLite; synthetic target admission, not ARM64 qualification."""

import json
import os
import subprocess
import sys
import unittest
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
            (self.target.root / "active.json").write_text(
                json.dumps({"manifest_sha256": digest}) + "\n"
            )
            self.assertEqual(
                self.target.verify_installed(PINS)["manifest_sha256"], digest
            )

    def test_ambiguous_and_oversized_pointers_deny_before_recovery(self) -> None:
        def interrupt(point: str) -> None:
            self.assertEqual(point, "after_install_intent")
            raise RuntimeError("synthetic interruption")

        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            self.target.install(
                b.VERSION,
                bundle("pending upgrade"),
                component_commits=PINS,
                expected_generation=3,
                checkpoint=interrupt,
            )
        digest = self.ids[2]
        pointer = self.target.root / "active.json"
        for payload in (
            f'{{"manifest_sha256":"{self.ids[0]}","manifest_sha256":"{digest}"}}'.encode(),
            b" " * 512 + b.canonical({"manifest_sha256": digest}),
            b'{"manifest_sha256":7}',
            b'[["manifest_sha256","' + digest.encode() + b'"]]',
        ):
            with self.subTest(size=len(payload)):
                pointer.write_bytes(payload)
                before = self.snapshot()
                checkpoint = Mock()
                with self.assertRaisesRegex(ValueError, "active pointer"):
                    self.target.recover(
                        b.VERSION, component_commits=PINS, checkpoint=checkpoint
                    )
                checkpoint.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_dangling_symlink_is_not_absence(self) -> None:
        pointer = self.target.root / "active.json"
        pointer.unlink()
        self.assertIsNone(self.target._observed())
        pointer.symlink_to(self.target.root / "missing-pointer")
        with self.assertRaisesRegex(ValueError, "active pointer"):
            self.target._observed()
        self.assertTrue(pointer.is_symlink())

    def test_fifo_pointer_is_rejected_without_waiting_for_a_writer(self) -> None:
        pointer = self.target.root / "active.json"
        pointer.unlink()
        os.mkfifo(pointer)
        result = subprocess.run(
            [
                sys.executable,
                "-O" if sys.flags.optimize else "-B",
                "-c",
                """
import sys
from unittest.mock import patch
from tools import development_bundle as b
with patch.object(b, "actual_target", return_value="linux/arm64"):
    target = b.DisposableTarget(sys.argv[1])
try:
    target._observed()
except ValueError as error:
    if "active pointer" not in str(error):
        raise
else:
    raise AssertionError("FIFO pointer accepted")
finally:
    target.close()
""",
                str(self.target.root),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(pointer.is_fifo())

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

    def test_rollback_predecessor_valid_after_recorded_intent(self) -> None:
        original = self.target.recover
        with patch.object(
            self.target, "recover", side_effect=RuntimeError("after intent")
        ):
            with self.assertRaisesRegex(RuntimeError, "after intent"):
                self.target.rollback(
                    b.VERSION, component_commits=PINS, expected_generation=3
                )
        self.assertEqual(self.target.state()["pending"], self.ids[1])
        self.assertEqual(
            self.target.verify_installed(PINS)["manifest_sha256"], self.ids[2]
        )
        self.assertEqual(
            original(b.VERSION, component_commits=PINS)["manifest_sha256"], self.ids[1]
        )


if __name__ == "__main__":
    unittest.main()
