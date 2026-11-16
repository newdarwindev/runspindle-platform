"""Mocked authoritative API observations; no dispatch or guest execution."""

import tempfile
import unittest
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet.mac_lifecycle import MacBlocked, MacGitHub, MacManager
from tools.runner_fleet.model import config


class Reader:
    def __init__(self, run: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        self.workflow_run = run
        self.jobs = jobs
        self.active = True
        self.fail = False
        self.calls: list[tuple[str, str]] = []

    def pages(self, path: str, key: str) -> list[dict[str, Any]]:
        self.calls.append((path, key))
        if self.fail:
            raise MacBlocked("API unavailable")
        if "/actions/runs/" in path and path.endswith("/jobs"):
            return self.jobs
        if path.endswith("status=queued"):
            return (
                [self.workflow_run]
                if self.active and self.workflow_run["status"] == "queued"
                else []
            )
        if path.endswith("status=in_progress"):
            return (
                [self.workflow_run]
                if self.active and self.workflow_run["status"] == "in_progress"
                else []
            )
        return []

    def get(self, path: str) -> dict[str, Any]:
        self.calls.append((path, "GET"))
        if self.fail:
            raise MacBlocked("API unavailable")
        return self.workflow_run


class PollingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = config()
        self.repo = "runspindle-platform"
        self.full = "newdarwindev/" + self.repo
        self.workflow_run: dict[str, Any] = {
            "id": 71,
            "status": "queued",
            "event": "workflow_dispatch",
            "head_branch": "ci/mac-probe",
            "head_sha": "a" * 40,
            "head_repository": {"full_name": self.full},
            "path": ".github/workflows/fleet-qualification.yml",
        }
        self.job: dict[str, Any] = {
            "id": 81,
            "name": "macbook-linux-arm64",
            "status": "queued",
            "created_at": "2026-11-15T10:00:00Z",
            "conclusion": None,
            "labels": self.cfg["lanes"]["macbook-linux-arm64"]["labels"],
            "runner_id": None,
            "runner_name": None,
        }
        self.now = 1794737100  # 2026-11-15T10:05:00Z

    def manager(self, directory: str) -> MacManager:
        return MacManager(self.cfg, directory)

    def test_queued_then_terminal_is_observed_without_claiming_qualification(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            reader = Reader(self.workflow_run, [self.job])
            first = manager.observe_jobs(reader, self.now)
            self.assertEqual(first["qualification"], "blocked")
            self.assertEqual(len(first["queue"]), 1)
            self.assertEqual(first["queue"][0]["run_id"], 71)
            self.assertEqual(manager.state.get("mac_watch_runs")[self.repo], [71])
            reader.active = False
            reader.workflow_run = self.workflow_run | {"status": "completed"}
            reader.jobs = [
                self.job
                | {
                    "status": "completed",
                    "conclusion": "failure",
                    "runner_id": 99,
                    "runner_name": "runspindle-fleet-synthetic",
                }
            ]
            second = manager.observe_jobs(reader, self.now + 60)
            self.assertEqual(second["queue"], [])
            self.assertEqual(second["terminal"][0]["conclusion"], "failure")
            observation = manager.state.mac_job_observation(self.repo, 81)
            assert observation is not None
            self.assertEqual(observation["outcome"], "completed")
            self.assertEqual(manager.state.get("mac_watch_runs")[self.repo], [])
            self.assertIn((f"repos/{self.full}/actions/runs/71", "GET"), reader.calls)

    def test_fork_untrusted_ref_workflow_event_and_wrong_job_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for run_change, job_change in (
                ({"head_repository": {"full_name": "fork/runspindle-platform"}}, {}),
                ({"head_branch": "feat/unreviewed"}, {}),
                ({"path": ".github/workflows/deploy.yml"}, {}),
                ({"event": "push"}, {}),
                ({"head_sha": "main"}, {}),
                ({}, {"name": "macbook-macos-arm64"}),
                ({}, {"labels": self.job["labels"][:-1]}),
            ):
                manager = self.manager(directory)
                reader = Reader(self.workflow_run | run_change, [self.job | job_change])
                self.assertEqual(manager.observe_jobs(reader, self.now)["queue"], [])
                self.assertEqual(manager.state.active(), [])

    def test_stale_queue_persistently_excluded_and_completion_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            reader = Reader(self.workflow_run, [self.job])
            first = manager.observe_jobs(reader, self.now + 2000)
            self.assertEqual(first["queue"], [])
            self.assertEqual(len(first["queue_expired"]), 1)
            observation = manager.state.mac_job_observation(self.repo, 81)
            assert observation is not None
            self.assertEqual(observation["outcome"], "queue_expired")
            self.assertEqual(manager.observe_jobs(reader, self.now)["queue"], [])
            reader.workflow_run = self.workflow_run | {"status": "completed"}
            reader.jobs = [
                self.job | {"status": "completed", "conclusion": "cancelled"}
            ]
            manager.observe_jobs(reader, self.now + 2100)
            observation = manager.state.mac_job_observation(self.repo, 81)
            assert observation is not None
            self.assertEqual(observation["outcome"], "completed")
            reader.workflow_run = self.workflow_run
            reader.jobs = [self.job]
            with self.assertRaisesRegex(MacBlocked, "cannot return to queue"):
                manager.observe_jobs(reader, self.now + 2200)

    def test_duplicate_ambiguous_identity_and_api_error_leave_ledger_intact(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            reader = Reader(self.workflow_run, [self.job, self.job])
            with self.assertRaisesRegex(MacBlocked, "duplicate"):
                manager.observe_jobs(reader, self.now)
            self.assertEqual(manager.state.get("mac_watch_runs"), None)
            reader.jobs = [self.job]
            reader.fail = True
            with self.assertRaisesRegex(MacBlocked, "API unavailable"):
                manager.observe_jobs(reader, self.now)
            self.assertEqual(manager.state.get("mac_watch_runs"), None)
            reader.fail = False
            manager.observe_jobs(reader, self.now)
            reader.workflow_run = self.workflow_run | {"status": "completed", "id": 72}
            reader.active = False
            with self.assertRaisesRegex(MacBlocked, "identity changed"):
                manager.observe_jobs(reader, self.now + 60)

    def test_terminal_binding_to_active_resource_uses_run_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            backend = Mock()
            backend.untracked.return_value = set()
            manager.backend = backend
            reader = Reader(self.workflow_run, [self.job])
            manager.reader = reader
            manager.clock = lambda: self.now
            candidate = self.job | {
                "repository": self.repo,
                "run_id": 71,
                "head_repository": self.full,
                "head_branch": self.workflow_run["head_branch"],
                "head_sha": self.workflow_run["head_sha"],
                "event": self.workflow_run["event"],
                "workflow_path": self.workflow_run["path"],
            }
            resource = manager.reserve_candidate(
                candidate,
                {"sha256": "b" * 64, "lane": "macbook-linux-arm64", "backend": "lima"},
            )
            reader.workflow_run = self.workflow_run | {"status": "completed"}
            reader.jobs = [self.job | {"status": "completed", "conclusion": "success"}]
            reader.active = False
            manager.observe_jobs(reader, self.now)
            observation = manager.state.active()[0]["details"]["github_observation"]
            self.assertEqual(observation["conclusion"], "success")
            self.assertEqual(observation["run_id"], resource["details"]["run_id"])
            self.assertEqual(manager.state.active()[0]["phase"], "reserved")
            self.assertEqual(backend.create.call_count, 0)

    def test_reservation_rechecks_authoritative_queued_identity(self) -> None:
        candidate = self.job | {
            "repository": self.repo,
            "run_id": 71,
            "head_repository": self.full,
            "head_branch": self.workflow_run["head_branch"],
            "head_sha": self.workflow_run["head_sha"],
            "event": self.workflow_run["event"],
            "workflow_path": self.workflow_run["path"],
        }
        image = {"sha256": "b" * 64, "lane": "macbook-linux-arm64", "backend": "lima"}
        cases = (
            (
                self.workflow_run
                | {"head_repository": {"full_name": "fork/runspindle-platform"}},
                [self.job],
            ),
            (self.workflow_run | {"head_sha": "c" * 40}, [self.job]),
            (self.workflow_run | {"status": "completed"}, [self.job]),
            (self.workflow_run, [self.job, self.job]),
            (self.workflow_run, [self.job | {"run_id": 999}]),
            (self.workflow_run, [self.job | {"head_sha": "c" * 40}]),
            (self.workflow_run, [self.job | {"created_at": "2026-11-15T08:00:00Z"}]),
        )
        for run, jobs in cases:
            with tempfile.TemporaryDirectory() as directory:
                reader = Reader(run, jobs)
                manager = MacManager(
                    self.cfg,
                    directory,
                    backend=Mock(),
                    reader=reader,
                    clock=lambda: self.now,
                )
                with self.assertRaises(MacBlocked):
                    manager.reserve_candidate(candidate, image)
                self.assertEqual(manager.state.active(), [])
        with tempfile.TemporaryDirectory() as directory:
            reader = Reader(self.workflow_run, [self.job])
            manager = MacManager(
                self.cfg,
                directory,
                backend=Mock(),
                reader=reader,
                clock=lambda: self.now,
            )
            manager.state.record_mac_job(self.repo, 81, 71, "a" * 40, "completed")
            with self.assertRaisesRegex(MacBlocked, "already terminal"):
                manager.reserve_candidate(candidate, image)
            reader.fail = True
            with self.assertRaisesRegex(MacBlocked, "API unavailable"):
                manager.reserve_candidate(candidate, image)
            self.assertEqual(manager.state.active(), [])

    def test_conflicting_job_source_blocks_poll_before_ledger_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            for mutation in ({"run_id": 999}, {"head_sha": "c" * 40}):
                reader = Reader(self.workflow_run, [self.job | mutation])
                with self.assertRaisesRegex(MacBlocked, "conflicts"):
                    manager.observe_jobs(reader, self.now)
                self.assertIsNone(manager.state.get("mac_watch_runs"))

    def test_api_pagination_and_malformed_limit_are_fail_closed(self) -> None:
        api = MacGitHub()
        calls: list[str] = []

        def page(path: str) -> dict[str, Any]:
            calls.append(path)
            return (
                {"jobs": [{"id": n} for n in range(100)]}
                if path.endswith("page=1")
                else {"jobs": [{"id": 101}]}
            )

        with patch.object(api, "get", side_effect=page):
            rows = api.pages(
                "repos/newdarwindev/runspindle-platform/actions/runs/7/jobs", "jobs"
            )
        self.assertEqual(len(rows), 101)
        self.assertEqual(len(calls), 2)
        with patch.object(api, "get", return_value={"wrong": []}):
            with self.assertRaisesRegex(MacBlocked, "malformed"):
                api.pages("runs", "jobs")
        with patch.object(api, "get", return_value={"jobs": [{}] * 100}):
            with self.assertRaisesRegex(MacBlocked, "limit"):
                api.pages("runs", "jobs")


if __name__ == "__main__":
    unittest.main()
