"""Offline proposal reconciliation; no GitHub or guest operations are executed."""

import copy
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet.demo_readiness import prepare, validate_candidate
from tools.runner_fleet.mac_lifecycle import MacBlocked, MacManager
from tools.runner_fleet.model import admissible_mac_job, choose_jobs, config
from tools.runner_fleet.service import qualify


class MacPolicyBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = config()
        self.plan = prepare(self.active, {}, None, None)
        self.candidate = self.plan["candidate_config"]

    def job(
        self, lane: str, repository: str = "runspindle-demo-suite"
    ) -> dict[str, Any]:
        return {
            "id": 1,
            "run_id": 2,
            "repository": repository,
            "head_repository": "newdarwindev/" + repository,
            "head_branch": "ci/synthetic",
            "head_sha": "a" * 40,
            "workflow_path": ".github/workflows/fleet-qualification.yml",
            "event": "workflow_dispatch",
            "name": lane,
            "labels": self.active["lanes"][lane]["labels"],
        }

    def test_candidate_validation_never_authorizes_scheduling(self) -> None:
        self.assertEqual(
            validate_candidate(self.candidate, self.plan["candidate_policy"]),
            self.candidate,
        )
        for lane, settings in self.active["lanes"].items():
            with self.subTest(lane=lane):
                with self.assertRaisesRegex(ValueError, "allowlist"):
                    choose_jobs(self.candidate, settings["host"], [self.job(lane)], [])
                self.assertEqual(
                    choose_jobs(self.active, settings["host"], [self.job(lane)], [])[0],
                    [],
                )

    def test_candidate_cannot_widen_mac_admission(self) -> None:
        for lane in ("macbook-linux-arm64", "macbook-macos-arm64"):
            self.assertFalse(admissible_mac_job(self.candidate, self.job(lane)))
            approved = self.job(lane, "runspindle-platform")
            self.assertTrue(admissible_mac_job(self.active, approved))
            self.assertEqual(
                choose_jobs(self.active, "macbook", [approved], [])[0], [approved]
            )

    def test_candidate_manager_rejected_before_state_or_external_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "must-not-exist"
            reader, backend = Mock(), Mock()
            with self.assertRaisesRegex(MacBlocked, "repository policy"):
                MacManager(self.candidate, state, reader=reader, backend=backend)
            self.assertFalse(state.exists())
            self.assertEqual(reader.mock_calls, [])
            self.assertEqual(backend.mock_calls, [])

    def test_caller_candidate_mutation_does_not_change_active_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader, backend = Mock(), Mock()
            reader.pages.return_value = []
            manager = MacManager(self.active, directory, reader=reader, backend=backend)
            before = copy.deepcopy(manager.cfg)
            self.active["repositories"].append("runspindle-demo-suite")
            self.active["lanes"]["macbook-linux-arm64"]["labels"].append("extra")
            self.assertEqual(manager.cfg, before)
            result = manager.observe_jobs(now=0)
            self.assertEqual(result["qualification"], "blocked")
            self.assertEqual(result["queue"], [])
            self.assertEqual(len(reader.pages.call_args_list), 16)
            self.assertFalse(
                any(
                    "runspindle-demo-suite" in call.args[0]
                    for call in reader.pages.call_args_list
                )
            )
            self.assertEqual(backend.mock_calls, [])
            manager.state.db.close()

    def test_changed_manager_policy_blocks_poll_and_reserve_before_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader, backend = Mock(), Mock()
            manager = MacManager(self.active, directory, reader=reader, backend=backend)
            manager.cfg["repositories"].append("runspindle-demo-suite")
            with self.assertRaisesRegex(MacBlocked, "repository policy"):
                manager.observe_jobs(now=0)
            with self.assertRaisesRegex(MacBlocked, "repository policy"):
                manager.reserve_candidate(self.job("macbook-macos-arm64"), {})
            self.assertEqual(reader.mock_calls, [])
            self.assertEqual(backend.mock_calls, [])
            self.assertEqual(manager.state.active(), [])
            self.assertIsNone(manager.state.get("mac_watch_runs"))
            manager.state.db.close()

    def test_original_memory_gate_and_inactive_proposal_remain_unchanged(self) -> None:
        lanes = self.active["lanes"].values()
        required = self.active["hosts"]["macbook"]["reserve_memory_gib"] + max(
            lane["memory_gib"] for lane in lanes if lane["host"] == "macbook"
        )
        self.assertEqual(required, 10)
        self.assertIs(self.plan["activation_allowed"], False)
        self.assertEqual(self.candidate["hosts"], self.active["hosts"])
        self.assertEqual(self.candidate["lanes"], self.active["lanes"])
        self.assertEqual(config(), self.active)

    def test_malformed_repository_rejected_before_authoritative_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader, backend = Mock(), Mock()
            manager = MacManager(self.active, directory, reader=reader, backend=backend)
            invalid_repositories: tuple[object, ...] = (None, 0, [])
            for repository in invalid_repositories:
                job = self.job("macbook-macos-arm64") | {"repository": repository}
                with self.assertRaisesRegex(MacBlocked, "Invalid Mac job identity"):
                    manager.reserve_candidate(job, {})
            self.assertEqual(reader.mock_calls, [])
            self.assertEqual(backend.mock_calls, [])
            self.assertEqual(manager.state.active(), [])
            manager.state.db.close()

    def test_strix_dispatch_keeps_inputless_workflow_compatibility(self) -> None:
        self.active["queue_timeout_seconds"] = 1
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github/workflows/fleet-qualification.yml"
        ).read_text()
        self.assertIn("default: strix-linux-x64", workflow)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "tools.runner_fleet.service.gh",
                side_effect=[
                    {"sha": "a" * 40},
                    {"workflow_runs": []},
                    {"workflow_runs": []},
                ],
            ),
            patch(
                "tools.runner_fleet.service.execute",
                return_value=subprocess.CompletedProcess([], 0, ""),
            ) as execute,
            patch(
                "tools.runner_fleet.service.time.monotonic",
                side_effect=[0, 2, 2, 2],
            ),
            patch(
                "tools.runner_fleet.service.time.sleep",
                side_effect=AssertionError("waited beyond queue deadline"),
            ),
        ):
            report = qualify(
                self.active,
                {"runspindle-core": "a" * 40},
                Path(directory),
                "ci/synthetic",
            )
            execute.assert_called_once_with(
                [
                    "gh",
                    "workflow",
                    "run",
                    "fleet-qualification.yml",
                    "-R",
                    "newdarwindev/runspindle-core",
                    "--ref",
                    "ci/synthetic",
                ]
            )
            self.assertEqual(report["workload_status"], "blocked")
