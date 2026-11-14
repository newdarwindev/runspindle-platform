"""Synthetic queue-campaign checks; never touch real services, guests or accounts."""

import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator, cast
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.runner_fleet import model as _model
from tools.runner_fleet import state as _state
from tools.runner_fleet.candidate import (
    CampaignDeadline,
    CandidateBackend,
    QueueManager,
    cleanup_campaign,
    receipts,
    run_campaign,
    time_budget,
    validate_plan,
    verify_executor,
    verify_image,
)

config = cast(Callable[[], dict[str, Any]], _model.config)
State: Any = _state.State


def plan() -> dict[str, Any]:
    cfg = config()
    return {
        "repositories": [cfg["owner"] + "/" + r for r in cfg["repositories"]],
        "acknowledge_repository_queue": True,
        "admission_seconds": 1200,
        "cleanup_seconds": 300,
        "image_path": "/var/lib/runspindle-fleet/images/candidate-"
        + "c" * 12
        + ".qcow2",
        "image_sha256": "d" * 64,
        "image_source_sha": "c" * 40,
        "controller_sha256": "e" * 64,
        "recipe_sha256": "f" * 64,
        "tool_closure_sha256": "0" * 64,
        "execution_source_sha": "1" * 40,
        "execution_controller_sha256": "2" * 64,
    }


class PlanTests(unittest.TestCase):
    def test_candidate_backend_refuses_preparation_before_effects(self) -> None:
        backend = CandidateBackend(Mock(), plan(), "synthetic-campaign")
        with (
            patch("tools.runner_fleet.candidate.verify_executor") as verify,
            patch("tools.runner_fleet.backend.Backend.create") as create,
        ):
            with self.assertRaises(ValueError):
                backend.create({}, prepare=True)
            verify.assert_not_called()
            create.assert_not_called()

    def test_exact_scope_and_bounds(self) -> None:
        p = plan()
        validate_plan(p, config())
        for key, value in [
            ("repositories", p["repositories"][:-1]),
            ("acknowledge_repository_queue", False),
            ("admission_seconds", 1201),
            ("admission_seconds", True),
            ("cleanup_seconds", 301),
            ("image_sha256", "main"),
            ("image_path", "/var/lib/runspindle-fleet/images/qualified.qcow2"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_plan(p | {key: value}, config())

    def test_execution_requires_second_ack_before_image_or_state(self) -> None:
        with (
            patch("tools.runner_fleet.candidate.State") as state,
            patch("tools.runner_fleet.candidate.verify_image") as verify,
        ):
            with self.assertRaises(ValueError):
                run_campaign(plan(), Path("/not-real"), False)
            state.assert_not_called()
            verify.assert_not_called()

    def test_wrong_image_and_source(self) -> None:
        p = plan()
        bound = {
            k: p[k]
            for k in (
                "image_source_sha",
                "controller_sha256",
                "recipe_sha256",
                "tool_closure_sha256",
            )
        }
        with (
            patch("tools.runner_fleet.candidate.Path.is_symlink", return_value=False),
            patch("tools.runner_fleet.candidate.Path.is_file", return_value=True),
            patch(
                "tools.runner_fleet.candidate.Path.stat",
                return_value=Mock(st_uid=0, st_mode=0o444),
            ),
            patch("tools.runner_fleet.candidate.digest", return_value="0" * 64) as sha,
            patch(
                "tools.runner_fleet.candidate.image_binding", return_value=bound
            ) as bind,
        ):
            with self.assertRaises(ValueError):
                verify_image(p, Path("/synthetic"))
            bind.assert_not_called()
            sha.return_value = p["image_sha256"]
            bind.return_value = bound | {"image_source_sha": "0" * 40}
            with self.assertRaises(ValueError):
                verify_image(p, Path("/synthetic"))

    def test_execution_controller_is_distinct_from_image_preparation(self) -> None:
        p = plan()
        with (
            patch(
                "tools.runner_fleet.candidate.execute",
                side_effect=[Mock(stdout=p["execution_source_sha"]), Mock(stdout="")],
            ),
            patch(
                "tools.runner_fleet.candidate.binding",
                return_value={"controller_sha256": p["execution_controller_sha256"]},
            ),
        ):
            result = verify_executor(p)
        self.assertNotEqual(result["source_sha"], p["image_source_sha"])
        self.assertNotEqual(result["controller_sha256"], p["controller_sha256"])
        with (
            patch(
                "tools.runner_fleet.candidate.execute",
                side_effect=[Mock(stdout=p["image_source_sha"]), Mock(stdout="")],
            ),
            patch(
                "tools.runner_fleet.candidate.binding",
                return_value={"controller_sha256": p["controller_sha256"]},
            ),
        ):
            with self.assertRaises(ValueError):
                verify_executor(p)

    def test_unsealed_image_denied_before_hash(self) -> None:
        with (
            patch("tools.runner_fleet.candidate.Path.is_symlink", return_value=False),
            patch("tools.runner_fleet.candidate.Path.is_file", return_value=True),
            patch(
                "tools.runner_fleet.candidate.Path.stat",
                return_value=Mock(st_uid=1000, st_mode=0o644),
            ),
            patch("tools.runner_fleet.candidate.digest") as sha,
        ):
            with self.assertRaises(ValueError):
                verify_image(plan(), Path("/synthetic"))
            sha.assert_not_called()


class QueueTests(unittest.TestCase):
    def manager(
        self, directory: str | Path, clock: Callable[[], float] = lambda: 1
    ) -> QueueManager:
        m = QueueManager(
            config(), directory, Mock(plan=plan(), campaign_id="campaign"), 1200, clock
        )
        self.addCleanup(m.state.db.close)
        return m

    def resource(self, m: QueueManager, phase: str = "running") -> dict[str, Any]:
        r = m.state.reserve({"repository": "runspindle-platform", "id": 10})
        m.state.update(
            r,
            phase,
            candidate_campaign="campaign",
            candidate_sha256=plan()["image_sha256"],
        )
        return cast(dict[str, Any], m.state.active()[0])

    def test_admission_honors_deadline_error_uncertainty_without_clearing_drain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            m.state.set("drained", True)
            self.assertTrue(m.can_admit())
            self.assertTrue(m.state.get("drained"))
            m.clock = lambda: 1200
            self.assertFalse(m.can_admit())
            m.clock = lambda: 1
            m.state.set("error", "synthetic")
            self.assertFalse(m.can_admit())
            m.state.set("error", None)
            self.resource(m, "uncertain")
            self.assertFalse(m.can_admit())

    def test_reservation_ownership_is_atomic_before_any_guest_creation(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            r = m.reserve_job({"repository": "runspindle-platform", "id": 44})
            self.assertEqual(r["details"]["candidate_campaign"], "campaign")
            reopened = State(d)
            self.assertEqual(
                reopened.active()[0]["details"]["candidate_campaign"], "campaign"
            )
            reopened.db.close()
            cast(Mock, m.backend.create).assert_not_called()

    def test_registration_phase_arms_conservative_independent_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            r = self.resource(m, "starting")
            m.state.update(r, "starting", boot_deadline=10**20)
            r = m.state.active()[0]
            cast(Mock, m.backend.ready).return_value = False
            m.arm_deadline = Mock()
            with patch("tools.runner_fleet.candidate.time.time", return_value=100):
                m.advance(r, [])
            self.assertEqual(
                m.state.active()[0]["details"]["candidate_job_deadline"], 1000
            )
            self.assertEqual(m.arm_deadline.call_count, 2)

    def test_normal_manager_stays_drained(self) -> None:
        from tools.runner_fleet.manager import Manager as _Manager

        Manager: Any = _Manager

        with tempfile.TemporaryDirectory() as d:
            m = Manager(config(), d, Mock())
            self.addCleanup(m.state.db.close)
            self.assertFalse(m.can_admit())

    def test_capacity_never_exceeds_two(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            jobs = [
                {
                    "repository": "runspindle-platform",
                    "id": n,
                    "labels": config()["lanes"]["strix-linux-x64"]["labels"],
                }
                for n in (1, 2, 3)
            ]
            with patch.object(m, "inventory", return_value=(jobs, [])):
                cast(Callable[[], None], m.tick)()
            self.assertEqual(len(m.state.active()), 2)
            self.assertEqual(cast(Mock, m.backend.create).call_count, 2)

    def test_actual_untrusted_claim_recorded_not_requested_identity(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            r = self.resource(m)
            job = {
                "id": 999,
                "run_id": 50,
                "run_attempt": 2,
                "head_sha": "f" * 40,
                "runner_name": r["runner_name"],
                "source_trusted": False,
                "status": "in_progress",
            }
            m.advance(r, [job])
            saved = m.state.active()[0]
            self.assertTrue(m.stopping)
            self.assertEqual(saved["phase"], "uncertain")
            self.assertEqual(saved["requested_job"], 10)
            self.assertEqual(saved["details"]["claimed_job"]["id"], 999)
            report = receipts(m.state, "campaign")[0]
            self.assertEqual(report["actual_claim"]["head_sha"], "f" * 40)

    def test_inventory_preserves_trust_for_admission_but_observes_other_runs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            m.cfg = m.cfg | {"repositories": ["runspindle-platform"]}
            base = {
                "status": "queued",
                "head_sha": "a" * 40,
                "head_branch": "ci/test",
                "run_attempt": 1,
                "head_repository": {"full_name": "newdarwindev/runspindle-platform"},
            }
            trusted = base | {"id": 1, "event": "push"}
            untrusted = base | {"id": 2, "event": "pull_request"}

            def listing(path: Any, key: Any) -> Any:
                if key == "workflow_runs":
                    return [trusted, untrusted] if "status=queued" in path else []
                identity = int(path.split("/")[path.split("/").index("runs") + 1])
                return [{"id": identity * 10, "status": "queued", "labels": []}]

            with patch("tools.runner_fleet.candidate.pages", side_effect=listing):
                queue, jobs = m.inventory()
            self.assertEqual([j["run_id"] for j in queue], [1])
            self.assertEqual({j["run_id"] for j in jobs}, {1, 2})

    def test_between_poll_completed_assignment_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            m.cfg = m.cfg | {"repositories": ["runspindle-platform"]}
            r = self.resource(m)
            observed = {
                "id": 9,
                "status": "completed",
                "head_sha": "b" * 40,
                "head_branch": "foreign",
                "run_attempt": 2,
                "event": "pull_request",
                "head_repository": {"full_name": "foreign/repo"},
            }

            def listing(path: Any, key: Any) -> Any:
                if key == "workflow_runs":
                    return [observed] if "created=" in path else []
                return [
                    {"id": 90, "status": "completed", "runner_name": r["runner_name"]}
                ]

            with patch("tools.runner_fleet.candidate.pages", side_effect=listing):
                queue, jobs = m.inventory()
            self.assertEqual(queue, [])
            self.assertEqual(jobs[0]["run_id"], 9)
            self.assertFalse(jobs[0]["source_trusted"])

    def test_watched_old_attempt_is_retained_when_successor_appears(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            m.cfg = m.cfg | {"repositories": ["runspindle-platform"]}
            m.assignment_watch["runspindle-platform"] = {(9, 1)}
            base = {
                "id": 9,
                "status": "in_progress",
                "head_sha": "b" * 40,
                "head_branch": "ci/test",
                "event": "push",
                "head_repository": {"full_name": "newdarwindev/runspindle-platform"},
            }
            paths = []

            def listing(path: Any, key: Any) -> Any:
                paths.append(path)
                if key == "workflow_runs":
                    return (
                        [base | {"run_attempt": 2}]
                        if "status=in_progress" in path
                        else []
                    )
                return [{"id": 90, "status": "completed"}]

            with (
                patch("tools.runner_fleet.candidate.pages", side_effect=listing),
                patch(
                    "tools.runner_fleet.candidate.gh",
                    return_value=base | {"run_attempt": 1},
                ),
            ):
                queue, jobs = m.inventory()
            self.assertEqual({j["run_attempt"] for j in jobs}, {1, 2})
            self.assertTrue(any("/attempts/1/jobs" in path for path in paths))
            self.assertTrue(any("/attempts/2/jobs" in path for path in paths))

    def test_advance_guest_exit_and_timeout_never_cancel_successor(self) -> None:
        for active in ("inactive", "active"):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as d:
                m = self.manager(d)
                r = self.resource(m)
                m.state.update(
                    r, "running", registered_at=time.time(), diagnostic_at=time.time()
                )
                r = m.state.active()[0]
                claim = {
                    "id": 999,
                    "run_id": 88,
                    "run_attempt": 1,
                    "head_sha": "a" * 40,
                    "source_trusted": True,
                    "runner_name": r["runner_name"],
                    "status": "in_progress",
                    "started_at": "2000-01-01T00:00:00Z",
                }
                with (
                    patch(
                        "tools.runner_fleet.candidate.gh",
                        return_value={
                            "id": 88,
                            "head_sha": "a" * 40,
                            "run_attempt": 2,
                            "status": "in_progress",
                        },
                    ) as api,
                    patch(
                        "tools.runner_fleet.manager.helper",
                        return_value={"ActiveState": active},
                    ),
                    patch.object(m, "remove_registration"),
                ):
                    m.advance(r, [claim])
                    self.assertEqual(m.state.active()[0]["phase"], "uncertain")
                    cleanup_campaign(m, "campaign")
                self.assertFalse(any(len(c.args) > 1 for c in api.call_args_list))
                cast(Mock, m.backend.destroy).assert_called_once()
                row = receipts(m.state, "campaign")[0]
                self.assertFalse(row["cleanup_verified"])
                self.assertNotEqual(row["operational_status"], "passed")

    def test_cleanup_attempts_both_sides_preserves_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            self.resource(m)
            cast(Mock, m.backend.destroy).side_effect = RuntimeError(
                "uncertain termination"
            )
            with patch.object(m, "remove_registration") as remove:
                cleanup_campaign(m, "campaign")
            remove.assert_called_once()
            self.assertEqual(m.state.active()[0]["phase"], "uncertain")

    def test_foreign_resource_never_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            self.resource(m)
            cleanup_campaign(m, "different-campaign")
            cast(Mock, m.backend.destroy).assert_not_called()

    def test_cleanup_never_posts_remote_cancel_even_matching_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            r = self.resource(m)
            m.state.update(
                r,
                "running",
                claimed_job={
                    "id": 999,
                    "run_id": 88,
                    "run_attempt": 2,
                    "head_sha": "a" * 40,
                    "status": "in_progress",
                },
            )
            with (
                patch(
                    "tools.runner_fleet.candidate.gh",
                    return_value={
                        "id": 88,
                        "head_sha": "a" * 40,
                        "run_attempt": 2,
                        "status": "in_progress",
                    },
                ) as api,
                patch.object(m, "remove_registration"),
            ):
                cleanup_campaign(m, "campaign")
            self.assertFalse(any(len(c.args) > 1 for c in api.call_args_list))
            self.assertEqual(m.state.active(), [])

    def test_changed_attempt_is_not_cancelled_and_remains_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            m = self.manager(d)
            r = self.resource(m)
            m.state.update(
                r,
                "running",
                claimed_job={
                    "id": 999,
                    "run_id": 88,
                    "run_attempt": 2,
                    "head_sha": "a" * 40,
                    "status": "in_progress",
                },
            )
            with (
                patch(
                    "tools.runner_fleet.candidate.gh",
                    return_value={
                        "id": 88,
                        "head_sha": "a" * 40,
                        "run_attempt": 3,
                        "status": "in_progress",
                    },
                ) as api,
                patch.object(m, "remove_registration"),
            ):
                cleanup_campaign(m, "campaign")
            self.assertFalse(any(len(c.args) > 1 for c in api.call_args_list))
            self.assertEqual(m.state.active()[0]["phase"], "uncertain")


class LifecycleTests(unittest.TestCase):
    def test_timer_interrupts_blocked_work_independently(self) -> None:
        with self.assertRaises(CampaignDeadline):
            with time_budget(0.01):
                time.sleep(1)

    def test_cleanup_deadline_preserves_uncertain_owned_reservation(self) -> None:
        calls = []

        @contextmanager
        def budget(seconds: float) -> Iterator[None]:
            calls.append(seconds)
            if len(calls) == 3:
                raise CampaignDeadline("synthetic cleanup deadline")
            yield

        def tick(manager: QueueManager) -> None:
            manager.reserve_job({"repository": "runspindle-platform", "id": 7})
            manager.stopping = True

        with tempfile.TemporaryDirectory() as d:
            state = State(d)
            state.set("drained", True)
            state.db.close()
            with (
                patch(
                    "tools.runner_fleet.candidate.verify_executor",
                    return_value={"source_sha": "1" * 40},
                ),
                patch("tools.runner_fleet.candidate.verify_image"),
                patch(
                    "tools.runner_fleet.candidate.idle_host",
                    return_value={"ActiveState": "inactive"},
                ),
                patch(
                    "tools.runner_fleet.candidate.protected_images",
                    return_value={"pair": "unchanged"},
                ),
                patch(
                    "tools.runner_fleet.candidate.execute",
                    return_value=Mock(stdout="ActiveState=inactive\n"),
                ),
                patch.object(QueueManager, "tick", tick),
            ):
                result = run_campaign(
                    plan(), d, True, sleep=lambda _: None, budget=budget
                )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["reason"], "cleanup_uncertain")
            self.assertFalse(result["ready_for_operator_restore"])
            self.assertEqual(result["receipts"][0]["phase"], "uncertain")

    def test_successful_claim_with_blocked_lifecycle_cannot_pass(self) -> None:
        clock_values = iter([0, 0, 0, 1201])

        def tick(manager: QueueManager) -> None:
            r = manager.reserve_job({"repository": "runspindle-platform", "id": 7})
            manager.state.update(
                r,
                "destroyed",
                cleanup_verified=True,
                operational_status="blocked",
                claimed_job={"id": 8, "conclusion": "success", "source_trusted": True},
            )

        with tempfile.TemporaryDirectory() as d:
            state = State(d)
            state.set("drained", True)
            state.db.close()
            with (
                patch(
                    "tools.runner_fleet.candidate.verify_executor",
                    return_value={"source_sha": "1" * 40},
                ),
                patch("tools.runner_fleet.candidate.verify_image"),
                patch(
                    "tools.runner_fleet.candidate.idle_host",
                    return_value={"ActiveState": "inactive"},
                ),
                patch(
                    "tools.runner_fleet.candidate.protected_images",
                    return_value={"pair": "unchanged"},
                ),
                patch(
                    "tools.runner_fleet.candidate.execute",
                    return_value=Mock(stdout="ActiveState=inactive\n"),
                ),
                patch.object(QueueManager, "tick", tick),
            ):
                result = run_campaign(
                    plan(),
                    d,
                    True,
                    clock=lambda: next(clock_values),
                    sleep=lambda _: None,
                    budget=lambda _: nullcontext(),
                )
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["ready_for_operator_restore"])

    def test_preflight_refusal_does_not_create_guest_or_change_drain(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = State(d)
            state.set("drained", True)
            state.db.close()
            with (
                patch(
                    "tools.runner_fleet.candidate.verify_executor",
                    return_value={"source_sha": "1" * 40},
                ),
                patch("tools.runner_fleet.candidate.verify_image"),
                patch(
                    "tools.runner_fleet.candidate.idle_host",
                    side_effect=RuntimeError("active service"),
                ),
                patch("tools.runner_fleet.candidate.CandidateBackend") as backend,
            ):
                with self.assertRaises(RuntimeError):
                    run_campaign(plan(), d, True, budget=lambda _: nullcontext())
                backend.return_value.create.assert_not_called()
            state = State(d)
            self.assertTrue(state.get("drained"))
            state.db.close()

    def test_shared_lock_refuses_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = State(d)
            with (
                state.lock(),
                patch("tools.runner_fleet.candidate.verify_executor"),
                patch("tools.runner_fleet.candidate.verify_image"),
                patch("tools.runner_fleet.candidate.idle_host") as idle,
            ):
                with self.assertRaises(BlockingIOError):
                    run_campaign(plan(), d, True, budget=lambda _: nullcontext())
                idle.assert_not_called()
            state.db.close()

    def test_empty_queue_reports_blocked_not_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            state = State(d)
            state.set("drained", True)
            state.db.close()
            times = iter([0, 0, 0, 1201])
            with (
                patch(
                    "tools.runner_fleet.candidate.verify_executor",
                    return_value={"source_sha": "1" * 40},
                ),
                patch("tools.runner_fleet.candidate.verify_image"),
                patch(
                    "tools.runner_fleet.candidate.idle_host",
                    return_value={"ActiveState": "inactive"},
                ),
                patch(
                    "tools.runner_fleet.candidate.protected_images",
                    return_value={"pair": "unchanged"},
                ),
                patch(
                    "tools.runner_fleet.candidate.execute",
                    return_value=Mock(stdout="ActiveState=inactive\n"),
                ),
                patch.object(QueueManager, "tick"),
            ):
                result = run_campaign(
                    plan(),
                    d,
                    True,
                    clock=lambda: next(times),
                    sleep=lambda _: None,
                    budget=lambda _: nullcontext(),
                )
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(result["ready_for_operator_restore"])
            self.assertTrue(result["drained"])
            self.assertEqual(result["rollout_qualification"], "not_claimed")


if __name__ == "__main__":
    unittest.main()
