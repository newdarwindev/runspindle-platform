"""Synthetic Mac fleet ledger tests; no VM or Actions execution."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.runner_fleet.mac_lifecycle import (
    MacBackend,
    MacBlocked,
    MacCommands,
    MacManager,
)
from tools.runner_fleet.model import config


class Commands:
    def __init__(self) -> None:
        self.instances: dict[str, dict[str, bool]] = {"tart": {}, "lima": {}}
        self.calls: list[tuple[str, str, str]] = []
        self.fail_stop = False

    def names(self, backend: str) -> dict[str, bool]:
        return dict(self.instances[backend])

    def stop(self, backend: str, name: str) -> None:
        self.calls.append(("stop", backend, name))
        if self.fail_stop:
            raise MacBlocked("lost stop reply")
        self.instances[backend][name] = False

    def delete(self, backend: str, name: str) -> None:
        self.calls.append(("delete", backend, name))
        self.instances[backend].pop(name)


class MacLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()
        self.now = datetime.now(timezone.utc).timestamp()
        self.job: dict[str, Any] = {
            "id": 41,
            "run_id": 40,
            "repository": "runspindle-platform",
            "labels": self.cfg["lanes"]["macbook-macos-arm64"]["labels"],
            "name": "macbook-macos-arm64",
            "head_repository": "newdarwindev/runspindle-platform",
            "head_branch": "ci/synthetic",
            "head_sha": "a" * 40,
            "event": "workflow_dispatch",
            "workflow_path": ".github/workflows/fleet-qualification.yml",
            "created_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
        }
        self.image = {
            "sha256": "b" * 64,
            "lane": "macbook-macos-arm64",
            "backend": "tart",
        }

    def manager(self, path: str) -> tuple[MacManager, Commands]:
        job = self.job

        class Reader:
            def get(self, path: str) -> dict[str, Any]:
                return {
                    "id": 40,
                    "status": "queued",
                    "event": job["event"],
                    "head_branch": job["head_branch"],
                    "head_sha": job["head_sha"],
                    "head_repository": {"full_name": job["head_repository"]},
                    "path": job["workflow_path"],
                }

            def pages(self, path: str, key: str) -> list[dict[str, Any]]:
                return [
                    dict(job, id=41, status="queued"),
                    dict(job, id=42, status="queued"),
                ]

        manager = MacManager(self.cfg, path, reader=Reader(), clock=lambda: self.now)
        commands = Commands()
        manager.backend = MacBackend(manager.state, commands)
        return manager, commands

    def test_admission_and_exact_image_binding_before_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, _ = self.manager(directory)
            for job, image in (
                (
                    self.job | {"head_repository": "fork/runspindle-platform"},
                    self.image,
                ),
                (
                    self.job | {"workflow_path": ".github/workflows/deploy.yml"},
                    self.image,
                ),
                (self.job | {"labels": self.job["labels"][:-1]}, self.image),
                (self.job, self.image | {"sha256": "latest"}),
                (self.job, self.image | {"backend": "lima"}),
            ):
                with self.assertRaises(MacBlocked):
                    manager.reserve_candidate(job, image)
                self.assertEqual(manager.state.active(), [])

    def test_single_slot_boot_denial_and_verified_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            resource = manager.reserve_candidate(self.job, self.image)
            self.assertEqual(resource["slot"], 0)
            self.assertEqual(resource["details"]["image_sha256"], "b" * 64)
            self.assertEqual(resource["details"]["lease_epoch"], 1)
            with self.assertRaises(MacBlocked):
                manager.reserve_candidate(self.job | {"id": 42}, self.image)
            with self.assertRaisesRegex(MacBlocked, "boot blocked"):
                manager.start(resource)
            self.assertEqual(manager.state.active()[0]["phase"], "uncertain")
            self.assertEqual(commands.calls, [])
            manager.reconcile()
            self.assertEqual(manager.state.active(), [])
            newer = manager.reserve_candidate(self.job | {"id": 42}, self.image)
            self.assertEqual(newer["details"]["lease_epoch"], 2)

    def test_owned_running_guest_stopped_deleted_and_absence_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            resource = manager.reserve_candidate(self.job, self.image)
            name = resource["details"]["guest_name"]
            marker = Path(directory) / "resources" / resource["id"] / "owner.json"
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            commands.instances["tart"][name] = True
            commands.instances["tart"]["personal-pilot"] = False
            manager.reconcile()
            self.assertEqual(
                commands.calls, [("stop", "tart", name), ("delete", "tart", name)]
            )
            self.assertEqual(manager.state.active(), [])
            self.assertEqual(commands.instances["tart"], {"personal-pilot": False})

    def test_uncertain_stop_and_untracked_guest_preserve_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            resource = manager.reserve_candidate(self.job, self.image)
            name = resource["details"]["guest_name"]
            commands.instances["tart"][name] = True
            commands.fail_stop = True
            with self.assertRaisesRegex(MacBlocked, "lost stop reply"):
                manager.reconcile()
            self.assertEqual(manager.state.active()[0]["phase"], "uncertain")
            with self.assertRaises(MacBlocked):
                manager.reserve_candidate(self.job | {"id": 42}, self.image)
            commands.fail_stop = False
            commands.instances["lima"]["runspindle-fleet-" + "c" * 32] = False
            with self.assertRaisesRegex(MacBlocked, "Untracked"):
                manager.reconcile()
            self.assertEqual(len(manager.state.active()), 1)
            self.assertEqual(
                commands.instances["lima"], {"runspindle-fleet-" + "c" * 32: False}
            )

    def test_marker_tamper_blocks_destructive_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            resource = manager.reserve_candidate(self.job, self.image)
            marker = Path(directory) / "resources" / resource["id"] / "owner.json"
            marker.write_text("{}")
            commands.instances["tart"][resource["details"]["guest_name"]] = True
            with self.assertRaisesRegex(MacBlocked, "marker changed"):
                manager.reconcile()
            self.assertEqual(commands.calls, [])
            self.assertEqual(manager.state.active()[0]["phase"], "uncertain")

    def test_same_name_other_backend_is_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            resource = manager.reserve_candidate(self.job, self.image)
            commands.instances["lima"][resource["details"]["guest_name"]] = False
            with self.assertRaisesRegex(MacBlocked, "Untracked"):
                manager.reconcile()
            self.assertEqual(len(manager.state.active()), 1)
            self.assertEqual(commands.calls, [])

    def test_orphan_guest_blocks_first_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            commands.instances["tart"]["runspindle-fleet-" + "d" * 32] = False
            with self.assertRaisesRegex(MacBlocked, "Untracked"):
                manager.reserve_candidate(self.job, self.image)
            self.assertEqual(manager.state.active(), [])
            self.assertEqual(commands.calls, [])

    def test_lima_inventory_empty_running_stopped_and_malformed(self) -> None:
        commands = MacCommands()
        with patch.object(commands, "run", return_value=""):
            self.assertEqual(commands.names("lima"), {})
        output = '[{"Name":"one","Status":"Running"},{"Name":"two","Status":"Stopped"}]'
        with patch.object(commands, "run", return_value=output):
            self.assertEqual(commands.names("lima"), {"one": True, "two": False})
        with patch.object(commands, "run", return_value="{broken"):
            with self.assertRaises(ValueError):
                commands.names("lima")

    def test_stale_resource_cannot_mutate_new_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            old = manager.reserve_candidate(self.job, self.image)
            manager.cleanup(old)
            newer = manager.reserve_candidate(self.job | {"id": 42}, self.image)
            with self.assertRaisesRegex(MacBlocked, "Stale"):
                manager.cleanup(old)
            self.assertEqual(manager.state.active()[0]["id"], newer["id"])
            self.assertEqual(commands.calls, [])

    def test_changed_caller_identity_cannot_control_owned_guest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, commands = self.manager(directory)
            resource = manager.reserve_candidate(self.job, self.image)
            commands.instances["tart"][resource["details"]["guest_name"]] = True
            for changed in (
                resource | {"repository": "runspindle-core"},
                resource
                | {"details": resource["details"] | {"image_sha256": "c" * 64}},
                resource | {"details": resource["details"] | {"lease_epoch": 2}},
            ):
                with self.assertRaisesRegex(MacBlocked, "Stale"):
                    manager.cleanup(changed)
                self.assertEqual(commands.calls, [])
            self.assertEqual(manager.state.active()[0]["id"], resource["id"])


if __name__ == "__main__":
    unittest.main()
