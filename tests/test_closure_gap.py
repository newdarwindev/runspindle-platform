from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.runner_fleet import closure_gap as gap


class ClosureGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock, self.manifest, self.cache = (
            self.root / p for p in ("Cargo.lock", "manifest.json", "cache")
        )
        self.registry = self.cache / "crates-io"
        self.registry.mkdir(parents=True)
        self.raw = b"synthetic archive bytes"
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.lock.write_text(
            f'[[package]]\nname="example"\nversion="1.0.0"\nsource="{gap.REGISTRY}"\nchecksum="{self.digest}"\n'
        )
        self.manifest.write_text('{"crate_archives":{}}')
        self.archive = self.registry / "example-1.0.0.crate"

    def test_missing_then_verified_then_pinned_never_claims_qualification(self) -> None:
        first = gap.audit(self.lock, self.manifest, self.cache)
        self.assertFalse(first["missing_from_manifest"][0]["locally_verified"])
        self.archive.write_bytes(self.raw)
        verified = gap.audit(self.lock, self.manifest, self.cache)
        self.assertEqual(verified["locally_verified_missing_bytes"], len(self.raw))
        self.assertFalse(verified["image_or_ci_qualification"])
        self.manifest.write_text(
            json.dumps({"crate_archives": {self.archive.name: self.digest}})
        )
        self.assertEqual(
            gap.audit(self.lock, self.manifest, self.cache)["registry_coverage"],
            "complete",
        )

    def test_corrupt_or_linked_archive_rejected(self) -> None:
        self.archive.write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            gap.audit(self.lock, self.manifest, self.cache)
        self.archive.unlink()
        self.archive.symlink_to(self.lock)
        with self.assertRaises(OSError):
            gap.audit(self.lock, self.manifest, self.cache)

    def test_duplicate_and_path_identity_rejected(self) -> None:
        original = self.lock.read_text()
        for text in (
            original + original,
            original.replace('name="example"', 'name="../escape"'),
        ):
            self.lock.write_text(text)
            with self.assertRaises(ValueError):
                gap.audit(self.lock, self.manifest, self.cache)

    def test_unsupported_source_not_leaked_or_treated_covered(self) -> None:
        self.lock.write_text(
            self.lock.read_text().replace(
                gap.REGISTRY, "git+https://private.invalid/SECRET"
            )
        )
        result = gap.audit(self.lock, self.manifest, self.cache)
        self.assertEqual(result["registry_coverage"], "incomplete")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_read_limits_and_duplicate_json_rejected(self) -> None:
        self.archive.write_bytes(self.raw)
        with patch.object(gap, "TOTAL_LIMIT", 1), self.assertRaises(ValueError):
            gap.audit(self.lock, self.manifest, self.cache)
        self.manifest.write_text('{"crate_archives":{},"crate_archives":{}}')
        with self.assertRaises(ValueError):
            gap.audit(self.lock, self.manifest, self.cache)

    def test_metadata_fifo_rejected_without_open_wait(self) -> None:
        import os

        self.lock.unlink()
        os.mkfifo(self.lock)
        with self.assertRaises(ValueError):
            gap.audit(self.lock, self.manifest, self.cache)

    def test_cli_structural_errors_are_sanitized(self) -> None:
        for lock, manifest in (
            ("package=[1]", '{"crate_archives":{}}'),
            (self.lock.read_text(), "[" * 1500 + "0" + "]" * 1500),
        ):
            self.lock.write_text(lock)
            self.manifest.write_text(manifest)
            output = io.StringIO()
            with (
                patch(
                    "sys.argv",
                    [
                        "audit",
                        "--lock",
                        str(self.lock),
                        "--manifest",
                        str(self.manifest),
                        "--cache",
                        str(self.cache),
                    ],
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(gap.main(), 2)
            self.assertEqual(
                json.loads(output.getvalue()),
                {"status": "rejected", "reason": "invalid_or_changed_inputs"},
            )
            self.assertNotIn(str(self.root), output.getvalue())
