"""Offline tests only: no registration, guest, backup or host changes."""

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet import registered_probe as p
from tools.runner_fleet.single_probe import STATE_DIRECTORY


class RegisteredProbeTests(unittest.TestCase):
    def test_cleanup_state_failure_still_stops_removes_retains(self) -> None:
        state, backend = Mock(), Mock()
        state.update.side_effect = RuntimeError("db")
        resource = {"slot": 0, "id": "owned"}
        with (
            patch.object(
                p, "helper", return_value={"ActiveState": "inactive"}
            ) as helper,
            patch.object(p, "remove_owned") as remove,
        ):
            result = p.cleanup(state, backend, resource, p.REPOS[0])
        helper.assert_any_call("stop", 0, "owned")
        remove.assert_called_once_with(p.REPOS[0], resource)
        backend.destroy.assert_called_once_with(resource)
        self.assertEqual(result["cleanup"], "blocked")

    def test_registration_failure_does_not_skip_stop_or_retention(self) -> None:
        state, backend = Mock(), Mock()
        resource = {"slot": 1, "id": "owned"}
        with (
            patch.object(
                p, "helper", return_value={"ActiveState": "inactive"}
            ) as helper,
            patch.object(p, "remove_owned", side_effect=RuntimeError("api")),
        ):
            result = p.cleanup(state, backend, resource, p.REPOS[1])
        helper.assert_any_call("stop", 1, "owned")
        backend.destroy.assert_called_once_with(resource)
        self.assertEqual(result["cleanup"], "blocked")
        self.assertEqual(state.update.call_args.args[1], "uncertain")

    def test_guard_failure_precedes_any_reservation(self) -> None:
        state = Mock()
        with (
            patch.object(p, "guarded_installation", side_effect=ValueError("guard")),
            patch.object(p, "Backend") as backend,
        ):
            with self.assertRaises(ValueError):
                p.run({"bindings": {"image_sha256": p.IMAGE_SHA}}, state, p.REPOS[0])
        state.reserve.assert_not_called()
        backend.assert_not_called()

    def test_outside_repository_rejected_before_guard(self) -> None:
        with patch.object(p, "guarded_installation") as guard:
            with self.assertRaises(ValueError):
                p.run({}, Mock(), "runspindle-tether")
        guard.assert_not_called()

    def test_remove_only_owned_and_recheck(self) -> None:
        resource = {"runner_name": "owned"}
        with (
            patch.object(
                p,
                "registrations",
                side_effect=[
                    [{"name": "other", "id": 1}, {"name": "owned", "id": 2}],
                    [{"name": "other", "id": 1}],
                ],
            ),
            patch.object(p, "gh") as gh,
        ):
            p.remove_owned(p.REPOS[0], resource)
        gh.assert_called_once_with(
            "repos/newdarwindev/runspindle-browser/actions/runners/2", "DELETE"
        )


class CapacityRecoveryTests(unittest.TestCase):
    def test_spacious_state_does_not_hide_full_fleet(self) -> None:
        import json

        pool = {
            "report": [
                {
                    "lv": [
                        {
                            "lv_size": str(579 * p.GIB),
                            "data_percent": "50",
                            "metadata_percent": "3",
                        }
                    ]
                }
            ]
        }
        with (
            patch.object(
                p,
                "storage_identity",
                return_value={
                    "state": 200 * p.GIB,
                    "fleet": 2 * p.GIB,
                    "slots": 2 * p.GIB,
                    "retained": 2 * p.GIB,
                },
            ),
            patch.object(Path, "read_text", return_value="MemAvailable: 20000000 kB\n"),
            patch.object(p, "execute", return_value=Mock(stdout=json.dumps(pool))),
        ):
            with self.assertRaisesRegex(ValueError, "reserve"):
                p.capacity(STATE_DIRECTORY)

    def test_wrong_actual_mount_rejected(self) -> None:
        import json
        import os
        import stat

        device = os.makedev(254, 7)
        info = Mock(st_mode=stat.S_IFBLK, st_rdev=device, st_dev=device)
        mounts = {
            "filesystems": [
                {"source": "/dev/other", "fstype": "ext4", "maj:min": "254:9"}
            ]
        }
        with (
            patch.object(Path, "stat", return_value=info),
            patch.object(Path, "resolve", lambda self, **kw: self),
            patch.object(p, "execute", return_value=Mock(stdout=json.dumps(mounts))),
        ):
            with self.assertRaisesRegex(ValueError, "mount identity"):
                p.storage_identity(STATE_DIRECTORY)

    def test_recovery_stops_removes_but_does_not_release_or_copy(self) -> None:
        from contextlib import nullcontext

        identity = "a" * 32
        resource = {
            "id": identity,
            "slot": 0,
            "runner_name": "runspindle-fleet-" + identity,
            "repository": p.REPOS[0],
            "details": {"manual_registered_probe": True, "image_sha256": p.IMAGE_SHA},
        }
        state = Mock(directory=STATE_DIRECTORY)
        state.lock.return_value = nullcontext()
        state.active.return_value = [resource]
        with (
            patch.object(p, "guarded_installation"),
            patch.object(p, "validate_state_directory"),
            patch.object(p, "manager_stopped"),
            patch.object(
                p, "helper", return_value={"ActiveState": "inactive"}
            ) as helper,
            patch.object(p, "remove_owned") as remove,
            patch.object(p, "Backend") as backend,
        ):
            result = p.recover(state, identity)
        helper.assert_any_call("stop", 0, identity)
        remove.assert_called_once_with(p.REPOS[0], resource)
        backend.assert_not_called()
        self.assertTrue(result["recovery_stopped"])
        self.assertFalse(result["snapshot_created"])
        self.assertFalse(result["capacity_released"])
        self.assertEqual(state.update.call_args.args[1], "uncertain")

    def test_recovery_wrong_binding_does_not_touch_resources(self) -> None:
        from contextlib import nullcontext

        state = Mock(directory=STATE_DIRECTORY)
        state.lock.return_value = nullcontext()
        state.active.return_value = [
            {"id": "a" * 32, "details": {}, "repository": p.REPOS[0]}
        ]
        with (
            patch.object(p, "guarded_installation"),
            patch.object(p, "validate_state_directory"),
            patch.object(p, "manager_stopped"),
            patch.object(p, "helper") as helper,
            patch.object(p, "remove_owned") as remove,
        ):
            with self.assertRaises(ValueError):
                p.recover(state, "a" * 32)
        helper.assert_not_called()
        remove.assert_not_called()


class HistoricalAssignmentTests(unittest.TestCase):
    def test_old_queued_run_completed_after_registration_is_observed(self) -> None:
        old_run = {"id": 10, "run_attempt": 1}
        job = {
            "id": 20,
            "runner_name": "owned",
            "status": "completed",
            "conclusion": "success",
        }

        def page(path: str, key: str) -> list[dict[str, Any]]:
            return [job] if path.endswith("/10/jobs") else []

        with (
            patch.object(p, "pages", side_effect=page),
            patch.object(p, "gh", return_value=old_run) as gh,
        ):
            result = p.observe_jobs(p.REPOS[0], "owned", "2026-11-18T00:00:00Z", [10])
        self.assertEqual([x["id"] for x in result], [20])
        self.assertEqual(result[0]["run_id"], 10)
        gh.assert_called_once_with(
            "repos/newdarwindev/runspindle-browser/actions/runs/10"
        )


class PendingRunObservationTests(unittest.TestCase):
    def test_existing_pending_run_is_observed_after_completion(self) -> None:
        run = {"id": 44305972724, "run_attempt": 1, "status": "completed"}
        job = {
            "id": 134807610979,
            "runner_name": "owned",
            "status": "completed",
            "conclusion": "failure",
        }

        def page(path: str, key: str) -> list[dict[str, Any]]:
            return [job] if path.endswith("/44305972724/jobs") else []

        with (
            patch.object(p, "pages", side_effect=page),
            patch.object(p, "gh", return_value=run),
        ):
            result = p.observe_jobs(
                p.REPOS[0], "owned", "2026-11-18T13:07:00Z", [44305972724]
            )
        self.assertEqual(result[0]["conclusion"], "failure")
        self.assertEqual(result[0]["id"], 134807610979)
