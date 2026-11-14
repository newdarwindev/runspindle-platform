"""A stale preflight cannot authorize manager resume after image or source drift."""

import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet import service
from runner_fleet.state import State


class ServiceBindingTests(unittest.TestCase):
    def test_resume_requires_exact_preflight_pair_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = root / "images"
            images.mkdir()
            image = images / "qualified.qcow2"
            image.write_bytes(b"synthetic image")
            bindings = {
                "controller_sha256": "a" * 64,
                "recipe_sha256": "b" * 64,
                "tool_closure_sha256": "c" * 64,
            }
            manifest = {
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                **bindings,
            }
            (images / "qualified.json").write_text(json.dumps(manifest))
            state = State(root)
            state.set("drained", True)
            state.set(
                "preflight",
                {
                    "status": "passed",
                    "observed_at": time.time(),
                    "checks": [
                        {"check": "image", "status": "passed", "detail": manifest}
                    ],
                },
            )
            with (
                patch.object(service, "ROOT", root),
                patch.object(service, "CODE", root),
                patch.object(service, "binding", return_value=bindings) as binding,
                patch.object(service, "helper"),
                patch.object(service, "observed", return_value={"status": "passed"}),
            ):
                self.assertEqual(
                    service.control({}, root, "resume")["status"], "passed"
                )
                self.assertFalse(state.get("drained"))
                state.set("drained", True)
                binding.return_value = bindings | {"recipe_sha256": "d" * 64}
                self.assertEqual(
                    service.control({}, root, "resume")["status"], "blocked"
                )
                self.assertTrue(state.get("drained"))
                binding.return_value = bindings
                (images / "qualified.json").write_text(
                    json.dumps(manifest | {"controller_sha256": "e" * 64})
                )
                self.assertEqual(
                    service.control({}, root, "resume")["status"], "blocked"
                )
                self.assertTrue(state.get("drained"))


if __name__ == "__main__":
    unittest.main()
