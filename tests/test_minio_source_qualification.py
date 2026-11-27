import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "minio_qualification",
    Path(__file__).resolve().parents[1] / "tools/minio_source_qualification.py",
)
if spec is None or spec.loader is None:
    raise RuntimeError("MinIO qualification helper unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class QualificationTests(unittest.TestCase):
    def test_wrong_revision_rejects_before_cache_or_build(self) -> None:
        with patch.object(
            module.subprocess, "check_output", return_value="a" * 40 + "\n"
        ):
            with self.assertRaisesRegex(ValueError, "revision/bytes mismatch"):
                module.qualify("/synthetic/source", "/synthetic/cache")

    def test_incomplete_dependency_download_does_not_qualify(self) -> None:
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

    def test_upstream_notice_binds_only_the_existing_module_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            readme = Path(temp) / "README.md"
            readme.write_text("synthetic README declares MIT")
            with patch.object(module, "LOCALEREADER_README_SHA256", module.sha(readme)):
                notice = module.localereader_notice(module.LOCALEREADER_PIN, readme)
                if notice is None:
                    self.fail("verified notice was rejected")
                self.assertEqual(
                    notice["notice_sha256"], module.LOCALEREADER_NOTICE_SHA256
                )
                self.assertEqual(
                    notice["notice_revision"], module.LOCALEREADER_NOTICE_REVISION
                )
                for key in module.LOCALEREADER_PIN:
                    with self.subTest(key=key):
                        changed = {**module.LOCALEREADER_PIN, key: "different"}
                        self.assertIsNone(module.localereader_notice(changed, readme))
                readme.write_text("changed README")
                self.assertIsNone(
                    module.localereader_notice(module.LOCALEREADER_PIN, readme)
                )

    def test_missing_or_modified_notice_keeps_qualification_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            readme = root / "README.md"
            readme.write_text("synthetic README declares MIT")
            with (
                patch.object(module, "ROOT", root),
                patch.object(module, "LOCALEREADER_README_SHA256", module.sha(readme)),
            ):
                self.assertIsNone(
                    module.localereader_notice(module.LOCALEREADER_PIN, readme)
                )
                notice = root / module.LOCALEREADER_NOTICE
                notice.parent.mkdir(parents=True)
                notice.write_text("synthetic substitute permission text")
                self.assertIsNone(
                    module.localereader_notice(module.LOCALEREADER_PIN, readme)
                )


if __name__ == "__main__":
    unittest.main()
