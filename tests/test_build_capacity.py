"""Build budgets include preserved originals and independent guest copies."""

import os
import pwd
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tools.runner_fleet import build_image, host_helper
from tools.runner_fleet.state import State

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

    def test_missing_mounts_or_root_fallback_refuse_before_state(self) -> None:
        root, state = Path("/synthetic/fleet"), Path("/synthetic/state")
        with patch.object(Path, "is_mount", return_value=False):
            with self.assertRaisesRegex(SystemExit, "mounts are required"):
                build_image.require_build_storage(root, state)
        with (
            patch.object(Path, "is_mount", return_value=True),
            patch.object(Path, "stat", return_value=SimpleNamespace(st_dev=1)),
            self.assertRaisesRegex(SystemExit, "no stale root fallback"),
        ):
            build_image.require_build_storage(root, state)

    def test_cleanup_reads_durable_boot_identity_instead_of_stale_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = State(temporary)
            initial = state.reserve({"repository": "synthetic", "id": -2})
            state.update(initial, "starting", image_sha256="a" * 64)
            observed = build_image.current_reservation(state, initial)
            self.assertNotIn("image_sha256", initial["details"])
            self.assertEqual(observed["details"]["image_sha256"], "a" * 64)
            state.update(initial, "destroyed")
            with self.assertRaisesRegex(RuntimeError, "missing or ambiguous"):
                build_image.current_reservation(state, initial)
            state.db.close()

    def test_memory_gate_preserves_host_reserve_and_kvm_denial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            meminfo = Path(temporary) / "meminfo"
            meminfo.write_text(f"MemAvailable: {18 * 1024**2} kB\n")
            with patch.object(os, "access", return_value=True):
                observed = build_image.require_build_runtime(6, 6, 2, meminfo)
                self.assertEqual(observed["required_bytes"], 18 * 1024**3)
                meminfo.write_text(f"MemAvailable: {18 * 1024**2 - 1} kB\n")
                with self.assertRaisesRegex(SystemExit, "host reserve"):
                    build_image.require_build_runtime(6, 6, 2, meminfo)
            with patch.object(os, "access", return_value=False):
                with self.assertRaisesRegex(SystemExit, "no privilege fallback"):
                    build_image.require_build_runtime(6, 6, 2, meminfo)


if __name__ == "__main__":
    unittest.main()
