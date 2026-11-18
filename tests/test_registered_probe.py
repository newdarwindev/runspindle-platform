"""Offline tests only: no registration, guest, backup or host changes."""

import unittest
from unittest.mock import Mock, patch
from tools.runner_fleet import registered_probe as p


class RegisteredProbeTests(unittest.TestCase):
    def test_cleanup_state_failure_still_stops_removes_retains(self):
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

    def test_registration_failure_does_not_skip_stop_or_retention(self):
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

    def test_guard_failure_precedes_any_reservation(self):
        state = Mock()
        with (
            patch.object(p, "guarded_installation", side_effect=ValueError("guard")),
            patch.object(p, "Backend") as backend,
        ):
            with self.assertRaises(ValueError):
                p.run({}, state, p.REPOS[0])
        state.reserve.assert_not_called()
        backend.assert_not_called()

    def test_outside_repository_rejected_before_guard(self):
        with patch.object(p, "guarded_installation") as guard:
            with self.assertRaises(ValueError):
                p.run({}, Mock(), "runspindle-tether")
        guard.assert_not_called()

    def test_remove_only_owned_and_recheck(self):
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
