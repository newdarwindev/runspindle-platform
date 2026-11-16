"""Negative policy tests with synthetic identities and temporary files only."""

import hashlib
import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.runner_fleet.isolated_campaign import (
    DISK_BYTES,
    GIB,
    Blocked,
    Ledger,
    Plan,
    Target,
    execution_gate,
    main,
)
from tools.runner_fleet.model import REPOSITORIES


def synthetic_plan(**changes: Any) -> Plan:
    targets = tuple(
        Target(
            "newdarwindev/" + r,
            "refs/heads/main",
            "a" * 40,
            "b" * 64,
            100 + i,
            1,
            1000 + i,
        )
        for i, r in enumerate(REPOSITORIES)
    )
    return Plan(
        targets,
        "runspindle-campaign-" + "c" * 32,
        "d" * 64,
        "e" * 40,
        "f" * 64,
        "0" * 64,
        "1" * 64,
        "2" * 40,
        "3" * 64,
        **changes,
    )


def race_admit(path: str, encoded: str, index: int) -> str:
    """Separate process and connection: exercise real SQLite serialization."""
    plan = Plan.parse(json.loads(encoded))
    ledger = Ledger(Path(path), plan)
    try:
        target = plan.targets[index]
        ledger.admit(
            target.repository,
            target.expected(plan.campaign_label) | {"status": "queued"},
            1024 * GIB,
            0,
        )
        return "admitted"
    except Blocked:
        return "blocked"
    finally:
        ledger.close()


def crash_capture(path: str, encoded: str) -> None:
    """Real abrupt exit after a durable charge; no Python finally executes."""
    import os

    plan = Plan.parse(json.loads(encoded))
    ledger = Ledger(Path(path), plan)
    with patch(
        "tools.runner_fleet.isolated_campaign.os.fsync",
        side_effect=lambda _: os._exit(73),
    ):
        ledger.capture(plan.targets[0].repository, b"keep crash evidence", 1024 * GIB)


class CampaignGuards(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "synthetic.sqlite"
        self.plan = synthetic_plan()
        self.free = self.plan.reserve_bytes + self.plan.allocation
        self.ledger = Ledger.create(self.path, self.plan, self.free)

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def observation(self, index: int = 0, **changes: Any) -> dict[str, Any]:
        return (
            self.plan.targets[index].expected(self.plan.campaign_label)
            | {"status": "queued"}
            | changes
        )

    def admit(self, index: int = 0) -> str:
        return self.ledger.admit(
            self.plan.targets[index].repository, self.observation(index), self.free, 0
        )

    def running(self, index: int = 0) -> dict[str, Any]:
        name = self.admit(index)
        observed = self.observation(index, runner_name=name, status="in_progress")
        self.ledger.assignment(self.plan.targets[index].repository, observed)
        return observed

    def finish(self, index: int = 0) -> None:
        observed = self.running(index)
        self.ledger.finish(
            self.plan.targets[index].repository,
            observed | {"status": "completed", "conclusion": "success"},
            {
                k: True
                for k in [
                    "guest_stopped",
                    "runner_removed",
                    "slot_empty",
                    "artifacts_verified",
                ]
            },
        )

    def reopen(self) -> None:
        self.ledger.close()
        self.ledger = Ledger(self.path, self.plan)

    def test_complete_coverage_is_guard_evidence_only(self) -> None:
        for i in range(8):
            self.finish(i)
        report = self.ledger.report()
        self.assertEqual(report["guard_coverage"], "complete")
        self.assertEqual(report["live_execution"], "disabled")
        self.assertIn("not_ci_qualification", report["evidence_class"])
        with self.assertRaises(Blocked):
            self.admit()

    def test_one_success_never_means_eight_repository_coverage(self) -> None:
        self.finish()
        self.assertEqual(self.ledger.report()["guard_coverage"], "blocked")

    def test_cap_survives_completion_restart_and_plan_edit(self) -> None:
        self.ledger.close()
        self.path = self.directory / "narrow.sqlite"
        self.plan = synthetic_plan(max_attempts=1)
        self.ledger = Ledger.create(self.path, self.plan, self.free)
        self.finish()
        self.reopen()
        with self.assertRaisesRegex(Blocked, "cap"):
            self.admit(1)
        with self.assertRaisesRegex(Blocked, "Plan changed"):
            Ledger(self.path, replace(self.plan, max_attempts=8))
        self.assertEqual(self.ledger.report()["attempts_consumed"], 1)

    def test_same_target_has_no_retry_after_completion(self) -> None:
        self.finish()
        with self.assertRaisesRegex(Blocked, "retries"):
            self.admit()

    def test_process_race_cannot_exceed_cap(self) -> None:
        self.ledger.close()
        self.path = self.directory / "race.sqlite"
        self.plan = synthetic_plan(max_attempts=1)
        self.ledger = Ledger.create(self.path, self.plan, self.free)
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            results = list(
                pool.map(
                    race_admit,
                    [str(self.path)] * 8,
                    [self.plan.encoded()] * 8,
                    range(8),
                )
            )
        self.assertEqual(results.count("admitted"), 1)
        self.assertEqual(self.ledger.report()["attempts_consumed"], 1)

    def test_process_race_cannot_exceed_two_slots(self) -> None:
        with ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            results = list(
                pool.map(
                    race_admit,
                    [str(self.path)] * 8,
                    [self.plan.encoded()] * 8,
                    range(8),
                )
            )
        self.assertEqual(results.count("admitted"), 2)

    def test_every_requested_identity_field_is_enforced(self) -> None:
        changes: dict[str, Any] = {
            "repository": "example/other",
            "head_repository": "example/fork",
            "ref": "refs/heads/ci/other",
            "head_sha": "f" * 40,
            "workflow_path": ".github/workflows/validation.yml",
            "workflow_sha256": "f" * 64,
            "event": "push",
            "run_id": 9999,
            "run_attempt": 2,
            "id": 9999,
            "name": "other",
            "labels": ["self-hosted", "linux", "x64", "runspindle-isolated", "strix"],
        }
        repo = self.plan.targets[0].repository
        for key, value in changes.items():
            with self.subTest(key=key):
                self.assertFalse(
                    self.ledger.exact(repo, self.observation(**{key: value}))
                )
        self.assertFalse(self.ledger.exact(repo, self.observation(run_attempt=True)))

    def test_wrong_requested_run_halts_before_reservation(self) -> None:
        with self.assertRaisesRegex(Blocked, "requested"):
            self.ledger.admit(
                self.plan.targets[0].repository,
                self.observation(run_attempt=2),
                self.free,
                0,
            )
        self.assertEqual(self.ledger.report()["attempts_consumed"], 0)
        self.reopen()
        with self.assertRaises(Blocked):
            self.admit(1)

    def test_unexpected_actual_assignment_stays_uncertain_after_restart(self) -> None:
        name = self.admit()
        with self.assertRaisesRegex(Blocked, "actual assignment"):
            self.ledger.assignment(
                self.plan.targets[0].repository,
                self.observation(run_id=9999, runner_name=name, status="in_progress"),
            )
        self.reopen()
        self.assertEqual(self.ledger.report()["attempts"][0][1], "uncertain")
        with self.assertRaises(Blocked):
            self.admit(1)

    def test_admission_deadline_does_not_create_attempt(self) -> None:
        with self.assertRaisesRegex(Blocked, "deadline"):
            self.ledger.admit(
                self.plan.targets[0].repository, self.observation(), self.free, 1200
            )
        self.assertEqual(self.ledger.report()["attempts_consumed"], 0)

    def test_restart_cannot_reset_persisted_admission_deadline(self) -> None:
        started = self.ledger.db.execute("SELECT started_ns FROM campaign").fetchone()[
            0
        ]
        self.reopen()
        self.ledger.clock = lambda: started + 1200 * 10**9
        with self.assertRaisesRegex(Blocked, "Persisted admission"):
            self.admit()
        self.assertEqual(self.ledger.report()["attempts_consumed"], 0)

    def test_restart_cannot_reset_job_deadline(self) -> None:
        name = self.admit()
        deadline = self.ledger.db.execute(
            "SELECT deadline_ns FROM attempts"
        ).fetchone()[0]
        self.reopen()
        self.ledger.clock = lambda: deadline
        with self.assertRaisesRegex(Blocked, "job deadline"):
            self.ledger.assignment(
                self.plan.targets[0].repository,
                self.observation(runner_name=name, status="in_progress"),
            )
        self.assertEqual(self.ledger.report()["attempts"][0][1], "uncertain")

    def test_backwards_clock_halts_instead_of_extending_admission(self) -> None:
        previous = self.ledger.db.execute(
            "SELECT last_seen_ns FROM campaign"
        ).fetchone()[0]
        self.ledger.clock = lambda: previous - 1
        with self.assertRaisesRegex(Blocked, "backwards"):
            self.admit()

    def test_insufficient_initial_space_has_no_file_effect(self) -> None:
        other = self.directory / "not-created.sqlite"
        with self.assertRaises(Blocked):
            Ledger.create(other, self.plan, self.free - 1)
        self.assertFalse(other.exists())

    def test_existing_ledger_is_never_recreated(self) -> None:
        before = self.path.stat().st_ino
        with self.assertRaises(FileExistsError):
            Ledger.create(self.path, self.plan, self.free)
        self.assertEqual(self.path.stat().st_ino, before)

    def test_unrelated_disk_consumption_halts_and_preserves_existing_capture(
        self,
    ) -> None:
        self.admit()
        repo = self.plan.targets[0].repository
        capture = self.ledger.capture(repo, b"complete original log\n", self.free)
        original = capture.read_bytes()
        with self.assertRaisesRegex(Blocked, "headroom"):
            self.ledger.account_disk(repo, 1, 30 * GIB - 1)
        self.assertEqual(capture.read_bytes(), original)
        self.reopen()
        with self.assertRaises(Blocked):
            self.admit(1)

    def test_disk_budget_cannot_be_exceeded_or_refunded(self) -> None:
        self.admit()
        repo = self.plan.targets[0].repository
        self.ledger.account_disk(repo, DISK_BYTES, self.free)
        with self.assertRaisesRegex(Blocked, "budget"):
            self.ledger.account_disk(repo, 1, self.free)
        self.reopen()
        self.assertEqual(self.ledger.report()["attempts"][0][2], DISK_BYTES)

    def test_log_exhaustion_preserves_all_previous_files_without_new_write(
        self,
    ) -> None:
        self.ledger.close()
        self.path = self.directory / "logs.sqlite"
        self.plan = synthetic_plan(log_bytes_per_attempt=16 * 1024**2)
        self.ledger = Ledger.create(self.path, self.plan, self.free)
        self.admit()
        repo = self.plan.targets[0].repository
        source = b"original evidence\n" + b"x" * (1024**2 - 18)
        originals = [self.ledger.capture(repo, source, self.free) for _ in range(15)]
        before = set(self.directory.glob("capture-*.log"))
        with self.assertRaisesRegex(Blocked, "budget"):
            self.ledger.capture(repo, source, self.free)
        self.assertEqual(set(self.directory.glob("capture-*.log")), before)
        for original in originals:
            self.assertEqual(original.read_bytes(), source)

    def test_capture_collision_never_overwrites_prior_evidence(self) -> None:
        self.admit()
        repo = self.plan.targets[0].repository
        identity = "f" * 32
        original = self.directory / ("capture-" + identity + ".log")
        original.write_bytes(b"existing complete log")
        with patch("tools.runner_fleet.isolated_campaign.uuid.uuid4") as generated:
            generated.return_value.hex = identity
            with self.assertRaisesRegex(Blocked, "Capture uncertain"):
                self.ledger.capture(repo, b"new capture", self.free)
        self.assertEqual(original.read_bytes(), b"existing complete log")

    def test_ledger_symlink_hardlink_and_shared_directory_are_rejected(self) -> None:
        alias = self.directory / "alias.sqlite"
        alias.symlink_to(self.path)
        with self.assertRaises(Blocked):
            Ledger(alias, self.plan)
        import os

        linked = self.directory / "linked.sqlite"
        os.link(self.path, linked)
        with self.assertRaises(Blocked):
            Ledger(linked, self.plan)
        shared = self.directory / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)  # Other fixture suites intentionally set umask 077.
        with self.assertRaises(Blocked):
            Ledger.create(shared / "new.sqlite", self.plan, self.free)

    def test_unknown_capture_write_retains_charge_and_blocks_after_restart(
        self,
    ) -> None:
        self.admit()
        repo = self.plan.targets[0].repository
        with patch(
            "tools.runner_fleet.isolated_campaign.os.fsync",
            side_effect=OSError("synthetic write failure"),
        ):
            with self.assertRaisesRegex(Blocked, "Capture uncertain"):
                self.ledger.capture(repo, b"preserve even partial output", self.free)
        self.assertEqual(len(list(self.directory.glob("capture-*.log"))), 1)
        self.reopen()
        self.assertGreater(self.ledger.report()["attempts"][0][3], 0)
        with self.assertRaises(Blocked):
            self.admit(1)

    def test_abrupt_process_death_leaves_capture_uncertain_and_fences_admission(
        self,
    ) -> None:
        self.admit()
        process = multiprocessing.get_context("spawn").Process(
            target=crash_capture, args=(str(self.path), self.plan.encoded())
        )
        process.start()
        process.join(timeout=10)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 73)
        self.reopen()
        with self.assertRaisesRegex(Blocked, "Unreconciled capture"):
            self.admit(1)
        captures = list(self.directory.glob("capture-*.log"))
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0].read_bytes(), b"keep crash evidence")
        self.assertEqual(self.ledger.report()["attempts"][0][1], "uncertain")

    def test_cleanup_uncertainty_keeps_slot_and_capture(self) -> None:
        observed = self.running()
        repo = self.plan.targets[0].repository
        capture = self.ledger.capture(repo, b"original evidence", self.free)
        with self.assertRaisesRegex(Blocked, "Cleanup uncertain"):
            self.ledger.finish(
                repo,
                observed | {"status": "completed", "conclusion": "success"},
                {
                    "guest_stopped": True,
                    "runner_removed": False,
                    "slot_empty": True,
                    "artifacts_verified": True,
                },
            )
        self.reopen()
        self.assertEqual(capture.read_bytes(), b"original evidence")
        self.assertEqual(self.ledger.report()["attempts"][0][1], "uncertain")
        with self.assertRaises(Blocked):
            self.admit(1)

    def test_captures_are_unique_and_hash_verified(self) -> None:
        self.admit()
        repo = self.plan.targets[0].repository
        a = self.ledger.capture(repo, b"same bytes", self.free)
        b = self.ledger.capture(repo, b"same bytes", self.free)
        self.assertNotEqual(a, b)
        self.assertEqual(
            hashlib.sha256(a.read_bytes()).digest(),
            hashlib.sha256(b.read_bytes()).digest(),
        )

    def test_live_gate_has_no_operator_or_synthetic_override(self) -> None:
        with self.assertRaisesRegex(Blocked, "unavailable"):
            execution_gate()
        plan_path = self.directory / "plan.json"
        plan_path.write_text(json.dumps(asdict(self.plan)))
        with (
            patch(
                "sys.argv", ["isolated-campaign", "--plan", str(plan_path), "--execute"]
            ),
            patch("tools.runner_fleet.isolated_campaign.Ledger") as store,
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(Blocked, "unavailable"):
                main()
            store.assert_not_called()

    def test_fleet_ledger_name_is_rejected_before_sqlite_or_file_effect(self) -> None:
        fleet = self.directory / "fleet.sqlite"
        with patch("tools.runner_fleet.isolated_campaign.sqlite3.connect") as connect:
            with self.assertRaisesRegex(Blocked, "outside"):
                Ledger.create(fleet, self.plan, self.free)
            with self.assertRaisesRegex(Blocked, "outside"):
                Ledger(fleet, self.plan)
            connect.assert_not_called()
        self.assertFalse(fleet.exists())

    def test_shared_labels_unknown_fields_missing_repos_and_lowered_guards_rejected(
        self,
    ) -> None:
        invalid = [
            replace(self.plan, campaign_label="strix"),
            replace(self.plan, max_attempts=9),
            replace(self.plan, concurrency=3),
            replace(self.plan, reserve_bytes=29 * GIB),
            replace(self.plan, targets=self.plan.targets[:-1]),
            replace(self.plan, max_attempts=True),
        ]
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                plan.validate()
        value = json.loads(self.plan.encoded()) | {"allow_live": True}
        with self.assertRaises(ValueError):
            Plan.parse(value)


if __name__ == "__main__":
    unittest.main()
