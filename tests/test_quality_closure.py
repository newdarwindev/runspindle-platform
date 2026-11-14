"""Synthetic negative cases for the staged quality-tool closure."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.quality import verify


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class QualityClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "source"
        self.downloads = root / "downloads"
        (self.source / "fleet").mkdir(parents=True)
        (self.downloads / "quality-wheels").mkdir(parents=True)
        (self.downloads / "quality-node").mkdir()
        requirements = b"ruff==0.14.5\n"
        (self.source / "requirements-quality.txt").write_bytes(requirements)
        (self.downloads / "requirements-quality.txt").write_bytes(requirements)
        wheels = []
        for number in range(8):
            name = f"synthetic_{number}-1.0-py3-none-any.whl"
            data = f"synthetic-{number}".encode()
            (self.downloads / "quality-wheels" / name).write_bytes(data)
            wheels.append({"filename": name, "sha256": sha(data), "bytes": len(data)})
        node = {}
        for name, field in (
            ("quality-node/package.json", "package_json_sha256"),
            ("quality-node/package-lock.json", "lock_sha256"),
            ("quality-node-modules.tar.gz", "modules_archive_sha256"),
        ):
            data = name.encode()
            (self.downloads / name).write_bytes(data)
            node[field] = sha(data)
            if field == "modules_archive_sha256":
                node["bytes"] = len(data)
        closure = {
            "schema_version": 1,
            "architecture": "linux/x86_64",
            "python": "3.12",
            "requirements_sha256": sha(requirements),
            "wheels": wheels,
            "node": node,
        }
        encoded = (json.dumps(closure) + "\n").encode()
        (self.source / "fleet/quality-closure.json").write_bytes(encoded)
        (self.source / "fleet/image-inputs.json").write_text(
            json.dumps(
                {
                    "quality": {
                        "closure": "fleet/quality-closure.json",
                        "closure_sha256": sha(encoded),
                    }
                }
            )
        )

    def test_verified_exact_closure(self) -> None:
        self.assertEqual(
            verify(self.source, self.downloads, "linux/x86_64")["status"],
            "verified",
        )

    def test_missing_extra_changed_and_wrong_architecture_fail(self) -> None:
        wheel = next((self.downloads / "quality-wheels").iterdir())
        original = wheel.read_bytes()
        wheel.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            verify(self.source, self.downloads, "linux/x86_64")
        wheel.write_bytes(original)
        (self.downloads / "quality-wheels/extra.whl").write_bytes(b"extra")
        with self.assertRaises(ValueError):
            verify(self.source, self.downloads, "linux/x86_64")
        (self.downloads / "quality-wheels/extra.whl").unlink()
        wheel.unlink()
        with self.assertRaises(ValueError):
            verify(self.source, self.downloads, "linux/x86_64")
        wheel.write_bytes(original)
        with self.assertRaises(ValueError):
            verify(self.source, self.downloads, "linux/aarch64")


if __name__ == "__main__":
    unittest.main()
