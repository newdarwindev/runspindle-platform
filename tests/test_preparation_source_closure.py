"""Exercise generated preparation cloud-init and imports without external effects."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet import backend as backend_module
from runner_fleet import state as state_module

# The existing backend/state interfaces have dynamic reservation JSON contracts.
backend: Any = backend_module
State: Any = state_module.State
ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_INPUTS = {
    "tools/runner_fleet/documents.py",
    "fleet/document-closure.json",
    "requirements-documents.txt",
    "fleet/DOCUMENT_NOTICES.md",
}


class PreparationSourceClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guest = self.root / "guest-source"
        self.guest.mkdir()
        fleet = self.root / "fleet"
        (fleet / "slots/0").mkdir(parents=True)
        image = self.root / "base.qcow2"
        image.write_bytes(b"synthetic image, never booted")
        state = State(self.root / "state")
        self.addCleanup(state.db.close)
        resource = state.reserve({"repository": "runspindle-platform", "id": -2})
        instance = backend.Backend(state, image)
        directory = instance.directory(resource)
        (directory / "key.pub").write_text("synthetic public key, never used")
        # Actual cloud-init generation runs. Every command and privileged helper
        # is replaced; no key generation, disk creation, guest or network action.
        with (
            patch.object(backend_module, "ROOT", fleet),
            patch.object(backend_module, "execute") as execute,
            patch.object(backend_module, "helper", return_value={}) as helper,
        ):
            instance.create(resource, prepare=True)
        self.assertEqual(execute.call_count, 3)
        self.assertEqual(
            [c.args[0] for c in helper.call_args_list], ["network", "observe", "start"]
        )
        data = (directory / "user-data").read_text().removeprefix("#cloud-config\n")
        cloud = json.loads(data)
        self.copied: set[str] = set()
        prefix = "/opt/runspindle-image-tools/"
        for item in cloud["write_files"]:
            if item["path"].startswith(prefix):
                relative = item["path"].removeprefix(prefix)
                self.copied.add(relative)
                path = self.guest / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item["content"])
                self.assertEqual(path.read_bytes(), (ROOT / relative).read_bytes())

    def imported_closure(self) -> subprocess.CompletedProcess[str]:
        # -I ignores cwd/PYTHONPATH/user site; only captured guest source is added.
        # This cannot silently fall back to the host repository's runner_fleet.
        script = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / 'tools'))
import runner_fleet.staging
from runner_fleet.documents import load
closure = load(root)
paths = {}
for name, module in sys.modules.items():
    if name == 'runner_fleet' or name.startswith('runner_fleet.'):
        path = pathlib.Path(module.__file__).resolve()
        paths[name] = str(path.relative_to(root))
print(json.dumps({'modules': paths, 'python': closure['python']}))
"""
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(self.guest)],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_cloud_init_carries_document_inputs_and_transitive_imports(self) -> None:
        self.assertTrue(DOCUMENT_INPUTS <= self.copied)
        result = self.imported_closure()
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["python"], "3.12.3")
        self.assertEqual(
            value["modules"],
            {
                "runner_fleet": "tools/runner_fleet/__init__.py",
                "runner_fleet.documents": "tools/runner_fleet/documents.py",
                "runner_fleet.quality": "tools/runner_fleet/quality.py",
                "runner_fleet.staging": "tools/runner_fleet/staging.py",
            },
        )

    def test_missing_verifier_or_transitive_helper_cannot_use_host_copy(self) -> None:
        for name in ("documents.py", "quality.py"):
            path = self.guest / "tools/runner_fleet" / name
            original = path.read_bytes()
            path.unlink()
            result = self.imported_closure()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ModuleNotFoundError", result.stderr)
            path.write_bytes(original)

    def test_missing_closure_or_requirements_fail_before_guest_install(self) -> None:
        for name in ("fleet/document-closure.json", "requirements-documents.txt"):
            path = self.guest / name
            original = path.read_bytes()
            path.unlink()
            result = self.imported_closure()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("staged quality archive missing or changed", result.stderr)
            path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
