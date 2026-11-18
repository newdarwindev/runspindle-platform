"""Real local SQLite lifecycle, synthetic catalog/backend, no external action."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import MagicMock

from tools.runner_fleet.automatic_admission import AutomaticAdmission
from tools.runner_fleet.automatic_catalog import QualifiedRecord
from tools.runner_fleet.automatic_policy import GIB, ImageRoute
from tools.runner_fleet.state import State


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = State(self.temp.name)
        self.addCleanup(self.state.db.close)
        route = ImageRoute(
            "runspindle-core", "a" * 64, "b" * 64, "c" * 64, "validation"
        )
        self.record = SimpleNamespace(
            route=route,
            source_sha="1" * 40,
            labels=lambda: [route.labels()[0] + "-source-" + "1" * 40],
            workflow_sha256="d" * 64,
            image_path=Path("/sealed/base"),
            manifest_path=Path("/sealed/manifest"),
            manifest_sha256="e" * 64,
            evidence_sha256="f" * 64,
            verify=MagicMock(),
        )
        self.capacity = MagicMock(
            return_value=dict(
                available_memory=13 * GIB,
                filesystem_free=55 * GIB,
                pool_free=55 * GIB,
                pool_metadata_basis_points=1,
                retention_allowance=25 * GIB,
            )
        )
        self.backend = MagicMock()
        self.backend.ready.return_value = True
        self.backend.create.side_effect = lambda resource, **kw: self.state.update(
            resource, "starting"
        )
        self.factory = MagicMock(return_value=self.backend)
        self.manager = AutomaticAdmission(
            self.state,
            self.capacity,
            lambda: (cast(QualifiedRecord, self.record),),
            self.factory,
        )
        self.job = dict(
            repository="runspindle-core",
            id=42,
            status="queued",
            event="push",
            head_branch="main",
            head_sha="1" * 40,
            head_repository="newdarwindev/runspindle-core",
            labels=self.record.labels(),
            run_id=7,
        )

    def admit(self) -> dict[str, Any] | None:
        return self.manager.admit(
            self.job, workflow_sha256="d" * 64, validation_manifest_sha256="c" * 64
        )

    def test_default_drain_has_no_external_effect(self) -> None:
        self.assertIsNone(self.admit())
        self.factory.assert_not_called()
        self.capacity.assert_not_called()

    def test_exact_route_creates_verify_only_and_registers_without_default_labels(
        self,
    ) -> None:
        self.state.set("drained", False)
        resource = self.admit()
        self.backend.create.assert_called_once_with(resource, network_mode="verify")
        starting = self.state.active()[0]
        self.manager.register_ready(
            cast(dict[str, Any], resource), "synthetic-registration-token"
        )
        self.backend.isolation.assert_called_once()
        self.backend.register.assert_called_once_with(
            starting,
            "newdarwindev/runspindle-core",
            "synthetic-registration-token",
            self.record.labels(),
            no_default_labels=True,
        )
        self.assertEqual(self.state.active()[0]["phase"], "running")
        self.assertIsNone(self.admit())

    def test_actual_inventory_shape_reaches_canonical_reservation(self) -> None:
        from unittest.mock import patch
        from tools.runner_fleet.manager import Manager

        collector = object.__new__(Manager)
        collector.cfg = {"owner": "newdarwindev", "repositories": ["runspindle-core"]}
        collector.state = self.state
        run = {
            "id": 7,
            "status": "queued",
            "event": "push",
            "head_branch": "main",
            "head_sha": "1" * 40,
            "head_repository": {"full_name": "newdarwindev/runspindle-core"},
            "path": ".github/workflows/validation.yml",
        }

        def pages(path: str, key: str) -> list[dict[str, Any]]:
            if key == "jobs":
                return [{"id": 42, "status": "queued", "labels": self.record.labels()}]
            return [run] if "status=queued" in path else []

        with patch("tools.runner_fleet.manager.pages", side_effect=pages):
            queue, _ = collector.inventory()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["head_repository"], "newdarwindev/runspindle-core")
        self.job = queue[0]
        self.state.set("drained", False)
        resource = self.admit()
        self.assertIsNotNone(resource)
        retained = self.state.active()[0]
        self.assertEqual(retained["details"]["requested_source"]["head_sha"], "1" * 40)
        self.assertEqual(retained["details"]["requested_source"]["run_id"], 7)
        self.backend.create.assert_called_once_with(resource, network_mode="verify")

    def test_malformed_or_foreign_inventory_authority_has_no_effect(self) -> None:
        self.state.set("drained", False)
        for change in (
            {"head_repository": "other/runspindle-core"},
            {"head_repository": {"full_name": "newdarwindev/runspindle-core"}},
            {"head_repository": None},
            {"event": "pull_request"},
            {"head_branch": "unreviewed"},
            {"head_branch": None},
            {"head_branch": 42},
            {"id": True},
        ):
            original = self.job
            self.job = original | change
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.admit()
            self.job = original
            self.assertEqual(self.state.active(), [])
        self.factory.assert_not_called()
        self.capacity.assert_not_called()

    def test_generic_route_or_workflow_change_cannot_reserve(self) -> None:
        self.state.set("drained", False)
        self.job["labels"] = ["self-hosted", "linux", "x64"]
        with self.assertRaises(ValueError):
            self.admit()
        self.assertEqual(self.state.active(), [])
        self.factory.assert_not_called()

    def test_later_source_with_same_workflow_and_manifest_is_not_admitted(self) -> None:
        self.state.set("drained", False)
        self.job["head_sha"] = "2" * 40
        with self.assertRaises(ValueError):
            self.admit()
        self.assertEqual(self.state.active(), [])
        self.factory.assert_not_called()

    def test_create_failure_retains_uncertain_capacity(self) -> None:
        self.state.set("drained", False)
        self.backend.create.side_effect = RuntimeError("network drift")
        with self.assertRaises(RuntimeError):
            self.admit()
        self.assertEqual(self.state.active()[0]["phase"], "uncertain")
        self.backend.register.assert_not_called()

    def test_catalog_change_or_drain_blocks_registration(self) -> None:
        self.state.set("drained", False)
        resource = self.admit()
        self.record.manifest_sha256 = "9" * 64
        with self.assertRaises(ValueError):
            self.manager.register_ready(cast(dict[str, Any], resource), "synthetic")
        self.backend.register.assert_not_called()
        self.state.set("drained", True)
        with self.assertRaises(ValueError):
            self.manager.register_ready(cast(dict[str, Any], resource), "synthetic")
        self.backend.register.assert_not_called()


class BackendOptionTests(unittest.TestCase):
    def test_no_default_labels_is_present_only_when_requested(self) -> None:
        from unittest.mock import patch

        from tools.runner_fleet import backend

        instance = object.__new__(backend.Backend)
        for enabled in (False, True):
            with (
                patch.object(instance, "directory", return_value=Path("/synthetic")),
                patch.object(
                    instance,
                    "ssh",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as ssh,
                patch.object(backend, "capture_bytes"),
            ):
                instance.register(
                    {"runner_name": "synthetic"},
                    "newdarwindev/runspindle-core",
                    "synthetic-token",
                    ["exact-route"],
                    no_default_labels=enabled,
                )
            script = ssh.call_args.args[1]
            self.assertEqual("--no-default-labels" in script, enabled)
            self.assertIn("--ephemeral --disableupdate", script)

    def test_automatic_wrong_image_rejected_before_helper(self) -> None:
        from unittest.mock import patch

        from tools.runner_fleet import backend

        instance = object.__new__(backend.Backend)
        instance.image = Path("/synthetic/base")
        resource = {
            "id": "a" * 32,
            "slot": 0,
            "details": {"automatic_fleet_v1": True, "image_sha256": "a" * 64},
        }
        with (
            patch.object(
                instance, "directory", return_value=Path("/synthetic/resource")
            ),
            patch.object(instance, "slot", return_value=Path("/synthetic/slot")),
            patch.object(backend, "digest", return_value="b" * 64),
            patch.object(backend, "helper") as helper,
        ):
            with self.assertRaisesRegex(RuntimeError, "reserved qualification"):
                instance.create(resource, network_mode="verify")
            helper.assert_not_called()


class LegacyOwnerTests(unittest.TestCase):
    def test_old_manager_cannot_advance_or_cleanup_new_owner(self) -> None:
        from unittest.mock import patch

        from tools.runner_fleet.manager import Manager

        manager = object.__new__(Manager)
        manager.state = MagicMock()
        resource = {
            "id": "a" * 32,
            "phase": "starting",
            "details": {"automatic_fleet_v1": True},
        }
        manager.state.active.return_value = [resource]
        manager.state.get.side_effect = (
            lambda key, default=None: False if key == "drained" else None
        )
        manager.backend = MagicMock()
        with patch("tools.runner_fleet.manager.helper") as helper:
            cast(Callable[..., Any], manager.advance)(resource, [])
            manager.cleanup(resource, "restart")
            self.assertFalse(manager.can_admit())
        helper.assert_not_called()
        manager.backend.assert_not_called()
        self.assertEqual(manager.backend.mock_calls, [])
        self.assertEqual(manager.state.update.call_count, 2)


if __name__ == "__main__":
    unittest.main()
