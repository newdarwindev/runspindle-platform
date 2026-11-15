"""Observe serialized guest provenance before any real infrastructure effect."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet import backend


class InstanceEmissionTests(unittest.TestCase):
    def exercise(
        self, *, missing: str | None = None, wrong_digest: bool = False
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "images/qualified.qcow2"
            image.parent.mkdir()
            image.write_bytes(b"synthetic image, never booted")
            manifest = {
                "sha256": backend.digest(image),
                "source_sha": "a" * 40,
                "controller_sha256": "b" * 64,
                "recipe_sha256": "c" * 64,
                "tool_closure_sha256": "d" * 64,
            }
            if missing:
                del manifest[missing]
            if wrong_digest:
                manifest["sha256"] = "0" * 64
            (image.parent / "qualified.json").write_text(json.dumps(manifest))
            (root / "slots/0").mkdir(parents=True)
            state = Mock(directory=root / "state")
            resource = {"id": "synthetic-resource", "slot": 0}
            machine = backend.Backend(state, image)
            (machine.directory(resource) / "key.pub").write_text("synthetic-key")
            original_stat = Path.stat

            def sealed_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
                result = original_stat(path, *args, **kwargs)
                if path == image:
                    fields = list(result)
                    fields[0] = 0o100444
                    fields[4] = 0
                    return os.stat_result(fields)
                return result

            with (
                patch.object(backend, "ROOT", root),
                patch.object(backend, "helper", return_value={}) as helper,
                patch.object(backend, "execute") as execute,
                patch.object(Path, "stat", sealed_stat),
            ):
                if missing or wrong_digest:
                    with self.assertRaises(RuntimeError):
                        machine.create(resource)
                    helper.assert_not_called()
                    execute.assert_not_called()
                    state.update.assert_not_called()
                    return
                machine.create(resource)
                cloud = json.loads(
                    (machine.directory(resource) / "user-data")
                    .read_text()
                    .removeprefix("#cloud-config\n")
                )
                records = [
                    row
                    for row in cloud["write_files"]
                    if row["path"] == "/etc/runspindle-instance.json"
                ]
                self.assertEqual(len(records), 1)
                self.assertEqual(
                    json.loads(records[0]["content"]),
                    {
                        "resource": resource["id"],
                        "slot": 0,
                        "image_sha256": manifest["sha256"],
                        "image_source_sha": manifest["source_sha"],
                        **{
                            key: manifest[key]
                            for key in (
                                "controller_sha256",
                                "recipe_sha256",
                                "tool_closure_sha256",
                            )
                        },
                    },
                )

    def test_serialized_guest_has_complete_manifest_binding(self) -> None:
        self.exercise()

    def test_missing_provenance_fails_before_effects(self) -> None:
        for field in (
            "source_sha",
            "controller_sha256",
            "recipe_sha256",
            "tool_closure_sha256",
        ):
            with self.subTest(field=field):
                self.exercise(missing=field)

    def test_wrong_image_bytes_fail_before_effects(self) -> None:
        self.exercise(wrong_digest=True)


if __name__ == "__main__":
    unittest.main()
