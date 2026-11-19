import unittest
from unittest.mock import Mock, patch

from tools.runner_fleet import direct_routing
from tools.runner_fleet.github_jobs import Attempt, Route, RoutingError


class DirectRoutingTest(unittest.TestCase):
    def setUp(self):
        self.route = Route("newdarwindev/runspindle-host", 100, 200, "a" * 40)
        self.attempt = Attempt(self.route, 300, 1, "b" * 40)
        repository = {"id": 100, "full_name": self.route.repository, "private": False}
        self.run = {
            "id": 300, "workflow_id": 200, "event": "push", "status": "queued",
            "head_branch": "ci/pilot", "head_sha": self.attempt.sha, "run_attempt": 1,
            "path": ".github/workflows/validation.yml", "repository": repository,
            "head_repository": repository, "actor": {"id": 42}, "triggering_actor": {"id": 42},
        }
        self.job = {
            "id": 401, "name": "validation / component", "status": "queued", "runner_id": 0,
            "run_id": 300, "run_attempt": 1, "head_sha": self.attempt.sha,
            "labels": ["runspindle-strix-direct-300-1-component"],
        }

    def read(self, method, path):
        self.assertEqual(method, "GET")
        if "/attempts/1/jobs?" in path:
            return {"total_count": 1, "jobs": [self.job]}
        if "/contents/" in path:
            return {"type": "file", "sha": self.route.workflow_blob}
        return self.run

    def test_only_approved_push_actor_and_exact_single_label(self):
        observed = direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})
        self.assertEqual(observed, (self.attempt, [self.job]))
        with self.assertRaisesRegex(RoutingError, "actor"):
            direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={43})
        self.job["labels"].append("self-hosted")
        with self.assertRaisesRegex(RoutingError, "identity or label"):
            direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})

    def test_listener_stays_offline_if_identity_cannot_be_saved(self):
        record = Mock(side_effect=OSError("ledger unavailable"))
        with (
            patch.object(direct_routing, "configure", return_value=52),
            patch.object(direct_routing, "start_listener") as listen,
        ):
            with self.assertRaises(OSError):
                direct_routing.enroll_job(
                    self.read, Mock(), Mock(), self.route, 300, 401,
                    "runspindle-fleet-" + "f" * 32, actor_ids={42},
                    verify_guest=Mock(), record_runner=record,
                )
            record.assert_called_once_with(52)
            listen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
