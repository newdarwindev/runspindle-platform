"""Reject unsafe and mismatched prepared archives before guest transfer."""

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.staging import safe_archive


class StagingTests(unittest.TestCase):
    def archive(self, name: str, data: bytes) -> Path:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "bundle.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        return path

    def test_crate_digest_and_inventory(self) -> None:
        data = b"synthetic crate"
        path = self.archive("registry/cache/index/example-1.0.crate", data)
        safe_archive(
            path, crate_hashes={"example-1.0.crate": hashlib.sha256(data).hexdigest()}
        )
        with self.assertRaisesRegex(ValueError, "Cargo crate inventory changed"):
            safe_archive(path, crate_hashes={"example-1.0.crate": "0" * 64})
        with self.assertRaisesRegex(ValueError, "unexpected Cargo crate archive"):
            safe_archive(path, crate_hashes={})

    def test_traversal_is_rejected(self) -> None:
        path = self.archive("../outside", b"synthetic")
        with self.assertRaisesRegex(ValueError, "unsafe staged archive member"):
            safe_archive(path)


if __name__ == "__main__":
    unittest.main()
