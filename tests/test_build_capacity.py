"""Build budgets include preserved originals and independent guest copies."""

import pwd
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tools.runner_fleet import build_image, host_helper

Build: Any = build_image


class BuildCapacityTests(unittest.TestCase):
    def test_budget_includes_three_disks_and_keeps_existing_floor(self) -> None:
        budget = 110 * 1024**3
        with patch.object(
            shutil, "disk_usage", return_value=SimpleNamespace(free=budget)
        ):
            observed = build_image.require_build_capacity(Path("/synthetic"), 30)
        self.assertEqual(observed["required_gib"], 110)
        with patch.object(
            shutil, "disk_usage", return_value=SimpleNamespace(free=budget - 1)
        ):
            with self.assertRaises(SystemExit):
                build_image.require_build_capacity(Path("/synthetic"), 30)

    def test_insufficient_capacity_has_no_state_or_guest_effect(self) -> None:
        with (
            patch.object(shutil, "disk_usage", return_value=SimpleNamespace(free=10)),
            patch.object(build_image, "State") as state,
            patch.object(build_image, "execute") as execute,
            self.assertRaises(SystemExit),
        ):
            Build.main()
        state.assert_not_called()
        execute.assert_not_called()

    def test_existing_serial_refuses_start_before_account_or_service_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slot = root / "slots/0"
            slot.mkdir(parents=True)
            log = slot / "serial.log"
            log.write_bytes(b"original serial bytes")
            with (
                patch.object(host_helper, "ROOT", root),
                patch.object(host_helper, "run") as run,
                patch.object(host_helper, "observe") as observe,
                patch.object(pwd, "getpwnam") as account,
                self.assertRaisesRegex(RuntimeError, "serial log retained"),
            ):
                host_helper.start(0, "a" * 32)
            run.assert_not_called()
            observe.assert_not_called()
            account.assert_not_called()
            self.assertEqual(log.read_bytes(), b"original serial bytes")


if __name__ == "__main__":
    unittest.main()
