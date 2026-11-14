"""Image identity changes when a recipe or controller input changes."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.identity import FIXED, RECIPE, binding


class IdentityTests(unittest.TestCase):
    def test_recipe_closure_and_controller_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*FIXED, *RECIPE):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            initial = binding(root)
            recipe = root / RECIPE[1]
            recipe.write_text("changed")
            changed = binding(root)
            self.assertNotEqual(
                initial["controller_sha256"], changed["controller_sha256"]
            )
            self.assertNotEqual(initial["recipe_sha256"], changed["recipe_sha256"])
            self.assertEqual(
                initial["tool_closure_sha256"], changed["tool_closure_sha256"]
            )
            recipe.write_text(RECIPE[1])
            closure = root / "fleet/quality-closure.json"
            closure.write_text("changed")
            changed = binding(root)
            self.assertNotEqual(
                initial["tool_closure_sha256"], changed["tool_closure_sha256"]
            )
            closure.unlink()
            with self.assertRaises(ValueError):
                binding(root)


if __name__ == "__main__":
    unittest.main()
