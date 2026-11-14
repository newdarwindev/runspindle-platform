import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "minio_qualification",
    Path(__file__).resolve().parents[1] / "tools/minio_source_qualification.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class QualificationTests(unittest.TestCase):
    def test_wrong_revision_rejects_before_cache_or_build(self):
        with patch.object(
            module.subprocess, "check_output", return_value="a" * 40 + "\n"
        ):
            with self.assertRaisesRegex(ValueError, "revision/bytes mismatch"):
                module.qualify("/synthetic/source", "/synthetic/cache")

    def test_incomplete_dependency_download_does_not_qualify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "modules.json").write_text(
                json.dumps(
                    {"Path": "synthetic/module", "Error": "artifact unavailable"}
                )
            )
            with patch.object(
                module.subprocess,
                "check_output",
                side_effect=[module.REVISION + "\n", module.REVISION + "\n", "", ""],
            ):
                with self.assertRaisesRegex(
                    ValueError, "incomplete dependency acquisition"
                ):
                    module.qualify(root, root)


if __name__ == "__main__":
    unittest.main()
