"""Check the public/private handoff without calling GitHub or launching guests."""

import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from urllib.parse import urlsplit

from tools.render_strix_workflow import private_workflow, public_workflow
from tools.runner_fleet import direct_routing
from tools.runner_fleet.direct_routing import queued_jobs as direct_jobs
from tools.runner_fleet.private_relay import (
    Attempt,
    CHECK_NAME,
    JOBS,
    PrivateWorkflow,
    Relay,
    RelayError,
    Route,
    observe_public,
)
from tools.runner_fleet.public_reporter import result
from tools.runner_fleet.state import State


class RelayTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = State(Path(directory.name))
        self.addCleanup(self.state.db.close)
        self.route = Route("newdarwindev/runspindle-host", 100, 200, "a" * 40)
        self.attempt = Attempt(self.route, 300, 1, "b" * 40)
        self.private = PrivateWorkflow("newdarwindev/runspindle-strix-exec", 123, 456, "main", "c" * 40, 999)
        self.relay = Relay(self.state, self.public_api, self.private_api, self.check_api, self.private, 10)
        self.run_visible = False
        self.lose_dispatch = False
        self.lose_publication = False
        self.posts = []
        self.checks = []
        self.public = {
            "id": 300, "workflow_id": 200, "head_branch": "ci/pilot",
            "event": "push", "status": "in_progress", "run_attempt": 1,
            "path": ".github/workflows/validation.yml", "head_sha": self.attempt.sha,
            "repository": self.public_repo(), "head_repository": self.public_repo(),
        }
        self.private_run = {
            "id": 789, "run_attempt": 1, "head_sha": self.private.sha, "workflow_id": 456,
            "event": "workflow_dispatch", "head_branch": "main",
            "path": ".github/workflows/strix-validation.yml", "display_title": self.attempt.key,
            "repository": {"id": 123}, "head_repository": {"id": 123},
            "actor": {"id": 999}, "triggering_actor": {"id": 999},
            "status": "completed", "conclusion": "success",
        }
        self.jobs = [
            {"name": name, "id": i, "run_id": 789, "run_attempt": 1,
             "head_sha": self.private.sha, "runner_id": i + 100, "status": "completed",
             "conclusion": "success", "labels": [f"runspindle-strix-789-1-{name}"]}
            for i, name in enumerate(JOBS, 1)
        ]

    def public_repo(self):
        return {"id": 100, "full_name": self.route.repository, "private": False}

    def public_api(self, method, path):
        self.assertEqual(method, "GET")
        if path == f"{self.attempt.public_path}/actions/runs/300":
            return self.public
        self.assertEqual(path, f"{self.attempt.public_path}/contents/.github/workflows/validation.yml?ref={self.attempt.sha}")
        return {"type": "file", "sha": self.route.workflow_blob}

    def private_api(self, method, path, expected=200, data=None):
        route = urlsplit(path).path
        if method == "POST":
            self.posts.append(path)
            self.assertEqual(route, f"{self.private.path}/actions/workflows/456/dispatches")
            self.assertEqual(expected, 200)
            self.assertEqual(data["inputs"]["request"], self.attempt.key)
            if self.lose_dispatch:
                raise RelayError("response lost")
            return {"workflow_run_id": 789}
        if route == self.private.path:
            return {"id": 123, "private": True, "full_name": self.private.repository}
        if route.endswith("/git/ref/heads/main"):
            return {"object": {"sha": self.private.sha}}
        if route.endswith("/workflows/456/runs"):
            rows = [self.private_run] if self.run_visible else []
            return {"total_count": len(rows), "workflow_runs": rows}
        if route.endswith("/attempts/1/jobs"):
            return {"total_count": len(self.jobs), "jobs": self.jobs}
        self.assertEqual(route, f"{self.private.path}/actions/runs/789")
        return self.private_run

    def check_api(self, method, path, expected=200, data=None):
        self.assertTrue(path.startswith(self.attempt.public_path + "/"))
        if method == "POST":
            self.assertEqual(expected, 201)
            self.posts.append(path)
            row = data | {"app": {"id": 10}, "id": 994}
            self.checks.append(row)
            if self.lose_publication:
                raise RelayError("response lost")
            return row
        return {"total_count": len(self.checks), "check_runs": self.checks}

    def reconciled(self):
        return {job["id"]: job["runner_id"] for job in self.jobs if job["runner_id"]}

    def test_exact_route_dispatch_execution_and_reporting(self):
        self.assertEqual(observe_public(self.public_api, self.route, 300), self.attempt)
        self.assertEqual(self.relay.dispatch(self.attempt), 789)
        self.assertEqual(self.relay.publish(self.attempt, self.reconciled()), 994)
        self.assertEqual(self.relay.publish(self.attempt, self.reconciled()), 994)
        self.assertEqual(len(self.posts), 2)
        fetch = lambda *_: {"total_count": len(self.checks), "check_runs": self.checks}
        self.assertTrue(result(fetch, self.route.repository, self.attempt.sha, 10, self.attempt.key))

    def test_lost_dispatch_reconciles_without_second_post(self):
        self.lose_dispatch = True
        with self.assertRaisesRegex(RelayError, "response lost"):
            self.relay.dispatch(self.attempt)
        with self.assertRaisesRegex(RelayError, "dispatch is unresolved"):
            self.relay.dispatch(self.attempt)
        self.run_visible = True
        self.assertEqual(self.relay.dispatch(self.attempt), 789)
        self.assertEqual(len(self.posts), 1)

    def test_lost_publication_reconciles_without_second_post(self):
        self.relay.dispatch(self.attempt)
        self.lose_publication = True
        with self.assertRaisesRegex(RelayError, "response lost"):
            self.relay.publish(self.attempt, self.reconciled())
        self.assertEqual(self.relay.publish(self.attempt, self.reconciled()), 994)
        self.assertEqual(len(self.posts), 2)

    def test_refuses_unapproved_event_or_changed_attempt(self):
        self.public["event"] = "pull_request"
        with self.assertRaisesRegex(RelayError, "approved route"):
            self.relay.dispatch(self.attempt)
        self.public["event"] = "push"
        self.public["run_attempt"] = 2
        with self.assertRaisesRegex(RelayError, "public attempt changed"):
            self.relay.dispatch(self.attempt)
        self.assertEqual(self.posts, [])

    def test_requires_exclusive_label_before_queuing(self):
        self.relay.dispatch(self.attempt)
        self.private_run["status"] = "in_progress"
        self.jobs = [self.jobs[0] | {"status": "queued", "runner_id": 0}]
        self.assertEqual(self.relay.queued_jobs(self.attempt), self.jobs)
        self.jobs[0]["labels"].append("self-hosted")
        with self.assertRaisesRegex(RelayError, "private job identity"):
            self.relay.queued_jobs(self.attempt)

    def test_direct_route_requires_approved_push_actor_and_job_label(self):
        self.public.update(status="queued", actor={"id": 42}, triggering_actor={"id": 42})
        job = {
            "id": 401, "name": "validation / component", "status": "queued",
            "run_id": 300, "run_attempt": 1, "head_sha": self.attempt.sha,
            "runner_id": 0, "labels": ["runspindle-strix-direct-300-1-component"],
        }

        def read(method, path):
            if "/attempts/1/jobs?" in path:
                return {"total_count": 1, "jobs": [job]}
            return self.public_api(method, path)

        self.assertEqual(direct_jobs(read, self.route, 300, actor_ids={42}), (self.attempt, [job]))
        with self.assertRaisesRegex(RelayError, "actor"):
            direct_jobs(read, self.route, 300, actor_ids={43})
        job["labels"].append("self-hosted")
        with self.assertRaisesRegex(RelayError, "identity or label"):
            direct_jobs(read, self.route, 300, actor_ids={42})

    def test_direct_enrollment_records_identity_before_listener(self):
        job = {"id": 401, "labels": ["runspindle-strix-direct-300-1-component"]}
        record = Mock(side_effect=OSError("ledger unavailable"))
        with (
            patch.object(direct_routing, "queued_jobs", return_value=(self.attempt, [job])),
            patch.object(direct_routing, "configure", return_value=52),
            patch.object(direct_routing, "start_listener") as listen,
        ):
            with self.assertRaises(OSError):
                direct_routing.enroll_job(
                    self.public_api, Mock(), Mock(), self.route, 300, 401,
                    "runspindle-fleet-" + "f" * 32, actor_ids={42},
                    verify_guest=Mock(), record_runner=record,
                )
            record.assert_called_once_with(52)
            listen.assert_not_called()

    def test_unconfirmed_cleanup_prevents_publication(self):
        self.relay.dispatch(self.attempt)
        with self.assertRaisesRegex(RelayError, "cleanup are unverified"):
            self.relay.publish(self.attempt, {})
        self.assertEqual(self.checks, [])

    def test_cancelled_or_missing_partition_reports_failure(self):
        self.relay.dispatch(self.attempt)
        self.jobs.pop()
        self.jobs[0]["conclusion"] = "cancelled"
        self.relay.publish(self.attempt, self.reconciled())
        self.assertEqual(self.checks[0]["conclusion"], "failure")

    def test_reporter_rejects_duplicate_results_and_ignores_old_attempt(self):
        settings = self.attempt.sha, 10, self.attempt.key
        row = {"id": 1, "name": CHECK_NAME, "head_sha": self.attempt.sha, "app": {"id": 10},
               "external_id": self.attempt.key, "status": "completed", "conclusion": "success"}
        old = row | {"external_id": self.attempt.key.replace(":300:1:", ":300:2:")}
        self.assertIsNone(result(lambda *_: {"total_count": 1, "check_runs": [old]}, "", *settings))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            result(lambda *_: {"total_count": 2, "check_runs": [row, row | {"id": 2}]}, "", *settings)

    def test_rendered_workflows_keep_the_public_gate_and_private_partitions(self):
        text = public_workflow()
        self.assertIn("name: validation / strix-linux-x64", text)
        self.assertNotIn("actions/checkout", text)
        body = text.split("          python3 -IB - <<'PY'\n", 1)[1].rsplit("          PY\n", 1)[0]
        compile("\n".join(line[10:] for line in body.splitlines()), "<workflow>", "exec")
        private = private_workflow()
        self.assertIn(f"needs: [{', '.join(JOBS[:-1])}]", private)
        for job in JOBS:
            self.assertIn(f"runspindle-strix-{{0}}-{{1}}-{job}", private)
            self.assertIn(f"tools/partition_validation.py {job}", private)

    def test_renderer_cli(self):
        observed = subprocess.check_output(
            [sys.executable, "-B", "-m", "tools.render_strix_workflow", "public"],
            cwd=Path(__file__).resolve().parents[1], text=True,
        )
        self.assertEqual(observed, public_workflow())


if __name__ == "__main__":
    unittest.main()
