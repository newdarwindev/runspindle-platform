import unittest
from unittest.mock import Mock, patch

from tools import strix_pilot
from tools.runner_fleet import direct_routing
from tools.runner_fleet.github_jobs import LAB, NEXT, Attempt, Route, RoutingError


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
            self.assertIn(self.route.workflow_path, path)
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

    def test_pending_run_admits_only_materialized_queued_jobs(self):
        self.run["status"] = "pending"
        observed = direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})
        self.assertEqual(observed, (self.attempt, [self.job]))
        self.job["status"] = "pending"
        self.assertEqual(
            direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42}),
            (self.attempt, []),
        )

    def test_lab_and_next_admit_only_their_verify_job_and_visibility(self):
        for repository, private in ((LAB, False), (NEXT, True)):
            with self.subTest(repository=repository):
                self.route = Route(repository, 100, 200, "a" * 40)
                attempt = Attempt(self.route, 300, 1, "b" * 40)
                source = {"id": 100, "full_name": repository, "private": private}
                self.run.update(
                    path=self.route.workflow_path, repository=source, head_repository=source,
                )
                self.job.update(name="verify", labels=["runspindle-strix-direct-300-1-verify"])
                self.assertEqual(
                    direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42}),
                    (attempt, [self.job]),
                )
                self.run["head_repository"] = source | {"private": not private}
                with self.assertRaisesRegex(RoutingError, "source repository identity"):
                    direct_routing.queued_jobs(self.read, self.route, 300, actor_ids={42})

    def test_cleanup_waits_for_busy_runner_then_proves_exact_removal(self):
        repo = self.route.repository
        name = "runspindle-fleet-" + "f" * 32
        label, = self.job["labels"]
        runner = {
            "id": 52, "name": name, "status": "offline", "busy": True,
            "labels": [{"name": label, "type": "custom"}],
        }
        inventory = {"total_count": 1, "runners": [runner]}
        with patch.object(strix_pilot, "api", return_value=inventory) as github:
            with self.assertRaises(strix_pilot.RunnerCleanupPending):
                strix_pilot.remove_owned_runner(repo, name, label, 52)
            self.assertTrue(all(call.args[0] == "GET" for call in github.call_args_list))
        runner["busy"] = False
        with patch.object(strix_pilot, "api", side_effect=[
            inventory, None, {"total_count": 0, "runners": []},
        ]) as github:
            strix_pilot.remove_owned_runner(repo, name, label, 52)
            self.assertEqual(github.call_args_list[1].args, (
                "DELETE", f"/repos/{repo}/actions/runners/52", 204,
            ))


if __name__ == "__main__":
    unittest.main()
