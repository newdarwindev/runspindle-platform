"""The document checker is mandatory in strict source validation, not native proof."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tools import partition_validation as gate


class MonitoringInventoryTests(unittest.TestCase):
    def test_monitoring_checker_is_mandatory_exactly_once(self) -> None:
        manifest, inventory, _, _ = gate.manifests()
        entries = [
            item
            for item in inventory["checks"]
            if item["id"] == "check-native-monitoring"
        ]
        self.assertEqual(
            entries,
            [
                {
                    "id": "check-native-monitoring",
                    "command": ["python3", "tools/check_native_monitoring.py"],
                    "required_files": ["tools/check_native_monitoring.py"],
                    "timeout_seconds": 300,
                }
            ],
        )
        assigned = [
            name
            for name, checks in manifest["partitions"].items()
            for check in checks
            if check == "check-native-monitoring"
        ]
        self.assertEqual(assigned, ["component"])
        native = next(
            item for item in inventory["checks"] if item["id"] == "maintained-mutations"
        )
        self.assertEqual(
            native["command"], ["python3", "tools/check_maintained_mutations.py"]
        )

    def test_missing_or_duplicate_monitoring_partition_fails_closed(self) -> None:
        manifest, inventory, _, _ = gate.manifests()
        for change in ("missing", "duplicate"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                mutated = copy.deepcopy(manifest)
                checks = mutated["partitions"]["component"]
                if change == "missing":
                    checks.remove("check-native-monitoring")
                else:
                    checks.append("check-native-monitoring")
                (root / "hygiene.json").write_text(json.dumps(inventory))
                (root / "validation-partitions.json").write_text(json.dumps(mutated))
                with self.assertRaisesRegex(
                    ValueError, "missing or duplicate mandatory"
                ):
                    gate.manifests(root)


if __name__ == "__main__":
    unittest.main()
