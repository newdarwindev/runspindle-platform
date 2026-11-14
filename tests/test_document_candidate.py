"""Synthetic document-candidate boundaries. No guests or external operations."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import Any, Callable, cast
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet import backend as legacy
from runner_fleet import candidate, document_candidate as dc, service
from runner_fleet import state as state_module
from runner_fleet.supervision import Deadline, Interrupted, UncertainProcess

SOURCE = Path(__file__).resolve().parents[1]
# Existing reservation/state APIs exchange dynamic JSON records.
State: Any = state_module.State


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def plan(root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "operation_id": "a" * 32,
        "execution_source_sha": "b" * 40,
        "binding": {
            "controller_sha256": "c" * 64,
            "recipe_sha256": "d" * 64,
            "tool_closure_sha256": "e" * 64,
        },
        "state_directory": str(root / "state"),
        "downloads": str(root / "downloads"),
        "source_bundles": str(root / "bundles"),
        "sources": {
            name: {"commit": "f" * 40, "sha256": "1" * 64, "bytes": 17}
            for name in dc.SOURCE_NAMES
        },
        "base_image": str(root / "images/qualified.qcow2"),
        "base_manifest": str(root / "images/qualified.json"),
        "base_sha256": "2" * 64,
        "base_manifest_sha256": "3" * 64,
        "helper_sha256": "4" * 64,
        "host_python": "3.12.14",
        "minimum_free_bytes": 56 * 1024**3,
        "protected": {str(root / "images/qualified.qcow2"): "2" * 64},
        "network_rules_sha256": sha(b"synthetic reviewed nft rules\n"),
        "document_closure_sha256": "5" * 64,
    }


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for relative in ("images", "state", "slots/0", "slots/1"):
            (self.root / relative).mkdir(parents=True)
        self.p = plan(self.root)
        for module in (dc, legacy, candidate, service):
            self.enterContext(patch.object(module, "ROOT", self.root))
        # Every accidental external command fails, including calls through a
        # legacy adapter. Specific tests replace only their expected mocks.
        for module in (dc, legacy, candidate, service):
            self.enterContext(
                patch.object(
                    module, "execute", side_effect=AssertionError("external command")
                )
            )
            self.enterContext(
                patch.object(
                    module, "helper", side_effect=AssertionError("external helper")
                )
            )

    def seed_state(self) -> None:
        mask = os.umask(0o077)
        os.umask(mask)
        self.addCleanup(os.umask, mask)
        state = State(self.p["state_directory"])
        state.set("drained", True)
        state.db.close()

    def rows(self) -> list[dict[str, Any]]:
        with sqlite3.connect(Path(self.p["state_directory"]) / "fleet.sqlite") as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row) | {"details": json.loads(row["details"])}
                for row in db.execute("SELECT * FROM resources ORDER BY created")
            ]

    def seed_protected(self) -> dict[str, bytes]:
        files = {
            "images/qualified.qcow2": b"synthetic qualified image",
            "images/qualified.json": b"synthetic qualified manifest",
            "images/qualified.previous.qcow2": b"synthetic rollback image",
            "images/qualified.previous.json": b"synthetic rollback manifest",
            "images/candidate-deadbeef1234.qcow2": b"synthetic existing candidate",
            "images/document-existing/candidate.qcow2": b"older document overlay",
            "state/candidate.json": b"synthetic shared candidate manifest",
        }
        for relative, data in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.p["protected"] = {
            str(self.root / relative): sha(data) for relative, data in files.items()
        }
        return {str(self.root / relative): data for relative, data in files.items()}


class PlanTests(Fixture):
    def test_plan_only_has_no_state_commands_or_writes(self) -> None:
        path = self.root / "plan.json"
        path.write_text(json.dumps(self.p))
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        opened = Path.open

        def read_only(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(character in mode for character in "wax+"):
                raise AssertionError("plan attempted a file write")
            return opened(path, mode, *args, **kwargs)

        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["document_candidate", "--plan", str(path)]),
            patch.object(dc, "State", side_effect=AssertionError("State constructed")),
            patch.object(dc, "verify", side_effect=AssertionError("runtime verify")),
            patch.object(dc, "run", side_effect=AssertionError("execution")),
            patch.object(Path, "open", read_only),
            patch.object(Path, "mkdir", side_effect=AssertionError("mkdir")),
            patch.object(Path, "chmod", side_effect=AssertionError("chmod")),
            patch.object(sqlite3, "connect", side_effect=AssertionError("database")),
            redirect_stdout(output),
        ):
            dc.main()
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], "plan_only")
        self.assertEqual(summary["qualification"], "not_run")
        self.assertEqual(summary["maximum_guests_total"], 2)
        self.assertEqual(summary["maximum_active_guests"], 1)
        for name in (
            "downloads",
            "registration",
            "queue_admission",
            "promotion",
            "restoration",
        ):
            self.assertIs(summary[name], False)
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertFalse(dc.paths(self.p)[0].exists())

    def test_plan_requires_exact_schema_inputs_and_paths(self) -> None:
        self.assertEqual(dc.validate_plan(self.p), self.p)
        changes = [
            ("schema_version", True),
            ("operation_id", "not-an-owned-identity"),
            ("execution_source_sha", "main"),
            ("state_directory", "/tmp/../state"),
            ("downloads", "relative"),
            ("host_python", "3.12"),
            ("minimum_free_bytes", True),
            ("minimum_free_bytes", 56 * 1024**3 - 1),
            ("base_image", str(self.root / "images/other.qcow2")),
            ("protected", {}),
            ("sources", {}),
            ("binding", {}),
        ]
        for name, value in changes:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                dc.validate_plan(self.p | {name: value})
        for change in ({"extra": True}, {"schema_version": None}):
            with self.assertRaises(ValueError):
                dc.validate_plan(self.p | change)
        invalid = copy.deepcopy(self.p)
        invalid["sources"]["root"]["bytes"] = True
        with self.assertRaises(ValueError):
            dc.validate_plan(invalid)

    def test_exact_caps_and_unique_outputs(self) -> None:
        expected = {
            "verify": 300,
            "prepare": 1500,
            "seal": 600,
            "prepare-cleanup": 300,
            "validate": 900,
            "validate-cleanup": 300,
            "final": 300,
        }
        self.assertEqual(dc.PHASES, expected)
        self.assertEqual(dc.TOTAL, 4200)
        self.assertEqual(sum(dc.PHASES.values()), dc.TOTAL)
        operation, image, manifest = dc.paths(self.p)
        other = dc.paths(self.p | {"operation_id": "b" * 32})
        self.assertEqual(manifest.parent, operation)
        self.assertNotEqual(manifest, self.root / "state/candidate.json")
        self.assertNotEqual(image, Path(self.p["base_image"]))
        self.assertTrue(
            all(
                left != right
                for left, right in zip((operation, image, manifest), other)
            )
        )


class InventoryTests(Fixture):
    def test_every_existing_image_and_shared_candidate_stays_unchanged(self) -> None:
        before = self.seed_protected()
        dc.protected(self.p)
        _, image, _ = dc.paths(self.p)
        image.parent.mkdir()
        image.write_bytes(b"only this operation's new output")
        dc.protected(self.p)
        self.assertEqual({name: Path(name).read_bytes() for name in before}, before)

    def test_changed_missing_or_added_image_is_rejected(self) -> None:
        before = self.seed_protected()
        target = self.root / "images/candidate-deadbeef1234.qcow2"
        target.write_bytes(b"modified")
        with self.assertRaises(ValueError):
            dc.protected(self.p)
        target.write_bytes(before[str(target)])
        target.unlink()
        with self.assertRaises(RuntimeError):
            dc.protected(self.p)
        target.write_bytes(before[str(target)])
        (self.root / "images/unreviewed.qcow2").write_bytes(b"new")
        with self.assertRaises(RuntimeError):
            dc.protected(self.p)

    def test_protected_symlink_is_rejected_even_with_matching_bytes(self) -> None:
        before = self.seed_protected()
        target = self.root / "images/qualified.previous.qcow2"
        replacement = self.root / "same-bytes"
        replacement.write_bytes(before[str(target)])
        target.unlink()
        target.symlink_to(replacement)
        with self.assertRaisesRegex(ValueError, "Linked"):
            dc.protected(self.p)

    def test_pinned_input_rejects_parent_and_dangling_symlinks(self) -> None:
        actual = self.root / "actual"
        actual.mkdir()
        (actual / "input").write_bytes(b"pinned")
        linked = self.root / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Linked"):
            dc.regular(linked / "input", sha(b"pinned"))
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "Linked"):
            dc.regular(dangling, "0" * 64)

    def test_collisions_fail_before_state_or_other_outputs(self) -> None:
        helper = self.root / "helper.py"
        helper.write_text("# synthetic helper\n")
        original_stat = Path.stat

        def root_helper_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            result = original_stat(path, *args, **kwargs)
            if path == helper:
                values = list(result)
                values[4] = 0
                return os.stat_result(values)
            return result

        for index, dangling in enumerate((False, True)):
            for output_index in (0, 1):
                p = self.p | {"operation_id": f"{index * 2 + output_index + 1:032x}"}
                operation, image, _ = dc.paths(p)
                output = operation if output_index == 0 else image.parent
                output.parent.mkdir(parents=True, exist_ok=True)
                if dangling:
                    output.symlink_to(self.root / "absent", target_is_directory=True)
                else:
                    output.mkdir()
                with (
                    self.subTest(output=output, dangling=dangling),
                    patch.object(dc, "verify_source"),
                    patch.object(dc, "regular"),
                    patch.object(legacy, "HELPER", str(helper)),
                    patch.object(Path, "stat", root_helper_stat),
                    patch("platform.python_version", return_value=p["host_python"]),
                    patch("platform.system", return_value="Linux"),
                    patch("platform.machine", return_value="x86_64"),
                    patch.object(dc, "State", side_effect=AssertionError("State")),
                ):
                    with self.assertRaisesRegex(RuntimeError, "already exists"):
                        dc.verify(p)
                self.assertFalse((operation / "plan.json").exists())


class LifecycleTests(Fixture):
    def test_read_only_verification_failure_precedes_state_and_fleet_writes(
        self,
    ) -> None:
        with (
            patch.object(
                dc, "initial_verify", side_effect=RuntimeError("prerequisites")
            ),
            patch.object(dc, "State", side_effect=AssertionError("State")) as state,
            patch.object(dc, "phase", side_effect=AssertionError("phase")) as phase,
        ):
            with self.assertRaisesRegex(RuntimeError, "prerequisites"):
                dc.run_bounded(self.p, 100.0, 4300.0)
        state.assert_not_called()
        phase.assert_not_called()
        self.assertFalse(dc.paths(self.p)[0].exists())
        self.assertFalse((self.root / "state/fleet.sqlite").exists())

    def test_reservation_committed_without_return_retains_uncertain_receipt(
        self,
    ) -> None:
        self.seed_state()
        state = State(self.p["state_directory"])
        reserve = state.reserve

        def lost_response(*args: Any, **kwargs: Any) -> dict[str, Any]:
            reserve(*args, **kwargs)
            raise RuntimeError("synthetic lost reservation response after commit")

        def final_only(p: dict[str, Any], name: str, *args: Any, **kwargs: Any) -> None:
            self.assertEqual(name, "final")
            dc.read_idle_state(Path(p["state_directory"]))

        with (
            patch.object(dc, "initial_verify"),
            patch.object(dc, "State", return_value=state),
            patch.object(state, "reserve", side_effect=lost_response),
            patch.object(dc, "stopped"),
            patch.object(dc, "phase", side_effect=final_only),
            patch.object(dc, "alarm", side_effect=lambda *a, **k: nullcontext()),
            patch.object(time, "monotonic", return_value=100.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "Candidate failed"):
                dc.run_bounded(self.p, 100.0, 4300.0)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["phase"], "destroyed")
        result = json.loads((dc.paths(self.p)[0] / "result.json").read_text())
        self.assertIs(result["cleanup_verified"], False)
        self.assertIs(result["remaining_capacity_uncertain"], True)

    def exercise(
        self,
        failure_phase: str | None = None,
        failure: BaseException | None = None,
        *,
        consume_caps: bool = False,
    ) -> tuple[list[tuple[str, float, bool]], dict[str, Any]]:
        self.seed_state()
        now = [100.0]
        calls: list[tuple[str, float, bool]] = []
        total_end = now[0] + dc.TOTAL

        def verify(_: dict[str, Any], end: float) -> None:
            self.assertEqual(end, 100.0 + dc.PHASES["verify"])
            if consume_caps:
                now[0] += dc.PHASES["verify"] - 1

        def phase(
            p: dict[str, Any],
            name: str,
            resource: dict[str, Any] | None,
            end: float,
            *,
            cleanup: bool = False,
        ) -> None:
            calls.append((name, end, cleanup))
            self.assertGreater(end, now[0])
            self.assertLessEqual(end - now[0], dc.PHASES[name])
            reserve = 0 if name == "final" else (300 if cleanup else 600)
            self.assertLessEqual(end, total_end - reserve)
            rows = self.rows()
            active = [row for row in rows if row["phase"] != "destroyed"]
            self.assertLessEqual(len(active), 1)
            if name != "final":
                self.assertIsNotNone(resource)
                if resource is None:
                    raise AssertionError("Owned resource is required")
                self.assertEqual([row["id"] for row in active], [resource["id"]])
                self.assertIs(active[0]["details"]["cleanup_verified"], False)
                self.assertEqual(
                    active[0]["details"]["document_operation"], p["operation_id"]
                )
                if name == "validate":
                    self.assertEqual(rows[0]["phase"], "destroyed")
                    self.assertIs(rows[0]["details"]["cleanup_verified"], True)
                    self.assertNotEqual(rows[0]["id"], resource["id"])
            if consume_caps:
                now[0] = end - 1
            if name == failure_phase:
                raise (
                    failure
                    if failure is not None
                    else RuntimeError("synthetic failure")
                )

        with (
            patch.object(dc, "initial_verify", side_effect=verify),
            patch.object(dc, "supervise", side_effect=AssertionError("real worker")),
            patch.object(dc, "stopped"),
            patch.object(dc, "phase", side_effect=phase),
            patch.object(dc, "alarm", side_effect=lambda *a, **k: nullcontext()),
            patch.object(time, "monotonic", side_effect=lambda: now[0]),
        ):
            if failure_phase is None:
                result = dc.run_bounded(self.p, 100.0, total_end)
            else:
                with self.assertRaisesRegex(RuntimeError, "Candidate failed"):
                    dc.run_bounded(self.p, 100.0, total_end)
                result = json.loads((dc.paths(self.p)[0] / "result.json").read_text())
        return calls, result

    def test_exactly_two_sequential_owned_guests(self) -> None:
        calls, result = self.exercise()
        self.assertEqual(
            [row[0] for row in calls],
            [
                "prepare",
                "seal",
                "prepare-cleanup",
                "validate",
                "validate-cleanup",
                "final",
            ],
        )
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["requested_job"] for row in rows], [-3, -4])
        self.assertTrue(all(row["phase"] == "destroyed" for row in rows))
        self.assertTrue(all(row["details"]["cleanup_verified"] for row in rows))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            result["qualification"], "unregistered_candidate_evidence_only"
        )
        self.assertIs(result["remaining_capacity_uncertain"], False)

    def test_all_phase_caps_include_cleanup_and_final_reserves(self) -> None:
        _, result = self.exercise(consume_caps=True)
        self.assertLess(result["seconds"], dc.TOTAL)
        self.assertGreater(result["seconds"], dc.TOTAL - 10)

    def test_failed_preparation_is_cleaned_without_validation(self) -> None:
        calls, result = self.exercise("prepare")
        self.assertEqual(
            [row[0] for row in calls], ["prepare", "prepare-cleanup", "final"]
        )
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["phase"], "destroyed")
        self.assertEqual(result["status"], "failed")
        self.assertIs(result["cleanup_verified"], True)

    def test_interrupted_seal_is_cleaned_without_validation(self) -> None:
        calls, _ = self.exercise("seal", Interrupted("synthetic interruption"))
        self.assertEqual(
            [row[0] for row in calls], ["prepare", "seal", "prepare-cleanup", "final"]
        )
        self.assertEqual(self.rows()[0]["phase"], "destroyed")

    def test_failed_cleanup_retains_capacity_and_prevents_validation(self) -> None:
        calls, result = self.exercise("prepare-cleanup")
        self.assertNotIn("validate", [row[0] for row in calls])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["phase"], "uncertain")
        self.assertIs(self.rows()[0]["details"]["cleanup_verified"], False)
        self.assertIs(result["cleanup_verified"], False)
        self.assertIs(result["remaining_capacity_uncertain"], True)

    def test_interrupted_cleanup_retains_capacity_and_prevents_validation(self) -> None:
        calls, result = self.exercise("prepare-cleanup", Deadline("cleanup cap"))
        self.assertNotIn("validate", [row[0] for row in calls])
        self.assertEqual(self.rows()[0]["phase"], "uncertain")
        self.assertIs(result["remaining_capacity_uncertain"], True)

    def test_unknown_phase_process_never_claims_safe_cleanup(self) -> None:
        calls, result = self.exercise("prepare", UncertainProcess("unknown child"))
        self.assertEqual([row[0] for row in calls], ["prepare", "final"])
        self.assertEqual(self.rows()[0]["phase"], "uncertain")
        self.assertIs(result["cleanup_verified"], False)

    def test_failed_validation_cleans_second_guest_without_a_third(self) -> None:
        calls, result = self.exercise("validate")
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([row[0] for row in calls][-2:], ["validate-cleanup", "final"])
        self.assertTrue(all(row["phase"] == "destroyed" for row in self.rows()))
        self.assertEqual(result["status"], "failed")

    def test_final_verification_failure_is_not_reported_passed(self) -> None:
        _, result = self.exercise("final")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(row["phase"] == "destroyed" for row in self.rows()))


class CleanupTests(Fixture):
    def test_no_slot_file_deleted_before_stopped_account_is_proved(self) -> None:
        resource = {"id": "a" * 32, "slot": 0}
        slot = self.root / "slots/0"
        backend = Mock()
        backend.slot.return_value = slot
        backend.directory.return_value = self.root / "state"
        for name in ("disk.qcow2", "seed.iso", "serial.log"):
            (slot / name).write_bytes(name.encode())
        before = {path.name: path.read_bytes() for path in slot.iterdir()}
        good = {
            "resource_id": resource["id"],
            "ActiveState": "inactive",
            "MainPID": "0",
        }
        with (
            patch.object(dc, "helper", return_value=good),
            patch.object(dc, "execute", return_value=Mock(returncode=0)),
        ):
            with self.assertRaisesRegex(RuntimeError, "still has processes"):
                dc.owned_cleanup(backend, resource)
        self.assertEqual(
            {path.name: path.read_bytes() for path in slot.iterdir()}, before
        )

    def test_symlink_in_owned_slot_is_retained_without_deleting_target(self) -> None:
        resource = {"id": "a" * 32, "slot": 0}
        slot = self.root / "slots/0"
        backend = Mock()
        backend.slot.return_value = slot
        backend.directory.return_value = self.root / "state"
        target = self.root / "preserved-image"
        target.write_bytes(b"must remain untouched")
        (slot / "disk.qcow2").symlink_to(target)
        good = {
            "resource_id": resource["id"],
            "ActiveState": "inactive",
            "MainPID": "0",
        }
        with (
            patch.object(dc, "helper", return_value=good),
            patch.object(dc, "execute", return_value=Mock(returncode=1)),
        ):
            with self.assertRaisesRegex(RuntimeError, "Unexpected slot files"):
                dc.owned_cleanup(backend, resource)
        self.assertTrue((slot / "disk.qcow2").is_symlink())
        self.assertEqual(target.read_bytes(), b"must remain untouched")

    def test_foreign_owner_is_not_destroyed(self) -> None:
        resource = {"id": "a" * 32, "slot": 0}
        backend = Mock()
        with patch.object(dc, "helper", return_value={"resource_id": "b" * 32}):
            with self.assertRaisesRegex(RuntimeError, "matching helper identity"):
                dc.owned_cleanup(backend, resource)
        backend.destroy.assert_not_called()

    def test_unknown_observation_nonempty_slot_and_account_processes_block_cleanup(
        self,
    ) -> None:
        resource = {"id": "a" * 32, "slot": 0}
        slot = self.root / "slots/0"
        backend = Mock()
        backend.slot.return_value = slot
        backend.directory.return_value = self.root / "state"
        good = {
            "resource_id": resource["id"],
            "ActiveState": "inactive",
            "MainPID": "0",
        }
        for observed in (
            {"resource_id": resource["id"]},
            good | {"MainPID": "99"},
            good | {"resource_id": "b" * 32},
        ):
            with (
                self.subTest(observed=observed),
                patch.object(dc, "helper", side_effect=[good, good, observed]),
            ):
                with self.assertRaisesRegex(RuntimeError, "independently verified"):
                    dc.owned_cleanup(backend, resource)
        (slot / "unknown-file").write_text("retain")
        with (
            patch.object(dc, "helper", return_value=good),
            patch.object(dc, "execute", return_value=Mock(returncode=1)),
        ):
            with self.assertRaisesRegex(RuntimeError, "Unexpected slot files"):
                dc.owned_cleanup(backend, resource)
        self.assertEqual((slot / "unknown-file").read_text(), "retain")
        (slot / "unknown-file").unlink()
        for returncode in (0, 2):
            with (
                self.subTest(returncode=returncode),
                patch.object(dc, "helper", return_value=good),
                patch.object(dc, "execute", return_value=Mock(returncode=returncode)),
            ):
                with self.assertRaisesRegex(RuntimeError, "still has processes"):
                    dc.owned_cleanup(backend, resource)

    def test_cleanup_requires_independent_positive_observations(self) -> None:
        resource = {"id": "a" * 32, "slot": 0}
        backend = Mock()
        backend.slot.return_value = self.root / "slots/0"
        backend.directory.return_value = self.root / "state"
        good = {
            "resource_id": resource["id"],
            "ActiveState": "inactive",
            "MainPID": "0",
        }
        with (
            patch.object(dc, "helper", return_value=good) as helper,
            patch.object(dc, "execute", return_value=Mock(returncode=1)) as execute,
        ):
            dc.owned_cleanup(backend, resource)
        backend.destroy.assert_not_called()
        self.assertEqual(
            helper.call_args_list,
            [call("observe", 0), call("stop", 0, resource["id"]), call("observe", 0)],
        )
        execute.assert_called_once_with(["pgrep", "-u", "runspindle-vm0"], check=False)


class NetworkAndBackendTests(Fixture):
    def network_result(self, argv: list[str], **kwargs: Any) -> Mock:
        if argv[:3] == ["sudo", "-n", "nft"]:
            return Mock(stdout="synthetic reviewed nft rules\n", returncode=0)
        if argv[:2] == ["systemctl", "show"]:
            return Mock(stdout="ActiveState=active\nSubState=running\n", returncode=0)
        if argv[:4] == ["ip", "-j", "-d", "addr"]:
            slot = int(argv[-1][-1])
            return Mock(
                stdout=json.dumps(
                    [
                        {
                            "flags": ["UP"],
                            "linkinfo": {"info_kind": "tun"},
                            "addr_info": [
                                {"local": f"10.231.{slot}.1", "prefixlen": 30}
                            ],
                        }
                    ]
                ),
                returncode=0,
            )
        raise AssertionError(f"unexpected external command: {argv}")

    def test_network_is_observed_read_only(self) -> None:
        with patch.object(dc, "execute", side_effect=self.network_result) as execute:
            dc.existing_network(self.p)
        self.assertEqual(execute.call_count, 7)
        self.assertTrue(
            all(
                "start" not in c.args[0] and "stop" not in c.args[0]
                for c in execute.call_args_list
            )
        )

    def test_denied_nft_observation_fails_closed(self) -> None:
        with patch.object(
            dc, "execute", side_effect=PermissionError("synthetic denial")
        ) as execute:
            with self.assertRaises(PermissionError):
                dc.existing_network(self.p)
        self.assertEqual(execute.call_count, 1)

    def test_changed_network_rules_services_or_taps_are_refused(self) -> None:
        for target in ("rules", "service", "tap"):

            def changed(argv: list[str], **kwargs: Any) -> Mock:
                result = self.network_result(argv, **kwargs)
                if target == "rules" and argv[0] == "sudo":
                    result.stdout += "changed"
                elif target == "service" and argv[0] == "systemctl":
                    result.stdout = "ActiveState=inactive\nSubState=dead\n"
                elif target == "tap" and argv[0] == "ip":
                    result.stdout = "[]"
                return result

            with (
                self.subTest(target=target),
                patch.object(dc, "execute", side_effect=changed),
            ):
                with self.assertRaises(RuntimeError):
                    dc.existing_network(self.p)

    def test_each_create_reobserves_network_and_denial_never_starts_guest(self) -> None:
        Path(self.p["base_image"]).write_bytes(b"synthetic base")
        state = Mock(directory=self.root / "state")
        backend = dc.DocumentBackend(state, self.p, validation=False)
        with (
            patch.object(dc, "stopped") as stopped,
            patch.object(
                dc, "existing_network", side_effect=PermissionError("denied")
            ) as network,
            patch.object(legacy, "helper") as helper,
            patch.object(legacy, "execute") as execute,
        ):
            for identity in ("a" * 32, "b" * 32):
                with self.assertRaises(PermissionError):
                    cast(Callable[..., None], backend.create)(
                        {"id": identity, "slot": 0}, prepare=True
                    )
        self.assertEqual(stopped.call_count, 2)
        self.assertEqual(network.call_count, 2)
        helper.assert_not_called()
        execute.assert_not_called()

    def test_cloud_init_contains_exact_document_sources_with_import_closure(
        self,
    ) -> None:
        Path(self.p["base_image"]).write_bytes(b"synthetic base")
        resource = {"id": "a" * 32, "slot": 0}
        state = Mock(directory=self.root / "state")
        backend = dc.DocumentBackend(state, self.p, validation=False)

        def command(argv: list[str], **kwargs: Any) -> Mock:
            if argv[0] == "ssh-keygen":
                Path(argv[-1] + ".pub").write_text("ssh-ed25519 SYNTHETIC\n")
            elif argv[0] not in ("qemu-img", "cloud-localds"):
                raise AssertionError(f"unexpected command {argv}")
            return Mock(returncode=0, stdout="", stderr="")

        with (
            patch.object(dc, "stopped"),
            patch.object(dc, "existing_network"),
            patch.object(dc, "idle_slots"),
            patch.object(legacy, "execute", side_effect=command),
            patch.object(
                legacy,
                "helper",
                return_value={"ActiveState": "inactive", "MainPID": "0"},
            ) as helper,
        ):
            cast(Callable[..., None], backend.create)(resource, prepare=True)
        self.assertEqual(
            helper.call_args_list,
            [call("observe", 0), call("start", 0, resource["id"])],
        )
        directory = cast(Callable[..., Path], backend.directory)(resource)
        cloud = json.loads((directory / "user-data").read_text().split("\n", 1)[1])
        prefix = "/opt/runspindle-image-tools/"
        files = {
            row["path"][len(prefix) :]: row["content"]
            for row in cloud["write_files"]
            if row["path"].startswith(prefix)
        }
        self.assertEqual(set(files), set(dc.DOCUMENT_SOURCES))
        self.assertIn("tools/runner_fleet/quality.py", files)
        self.assertNotIn("tools/runner_fleet/prepare_image.sh", files)
        isolated = self.root / "isolated-source"
        for name, content in files.items():
            destination = isolated / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        script = (
            "import pathlib, sys; "
            f"sys.path.insert(0, {str(isolated / 'tools')!r}); "
            "from runner_fleet.documents import load; "
            f"result = load(pathlib.Path({str(isolated)!r})); "
            "assert len(result['wheels']) == 3; print('isolated imports passed')"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script],
            cwd=isolated,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "isolated imports passed")
        self.assertFalse(list(isolated.rglob("__pycache__")))


class OrdinaryFleetRejectionTests(Fixture):
    def overlay_manifest(self, image: Path) -> dict[str, Any]:
        return {
            "manifest_kind": "document-overlay-v1",
            "candidate_path": str(image),
            "sha256": sha(image.read_bytes()),
            "source_sha": "a" * 40,
            **self.p["binding"],
        }

    def test_actual_candidate_verification_rejects_document_overlay(self) -> None:
        image = self.root / "images/candidate-aabbccddeeff.qcow2"
        image.write_bytes(b"synthetic overlay")
        image.chmod(0o444)
        manifest = self.overlay_manifest(image)
        (self.root / "state/candidate.json").write_text(json.dumps(manifest))
        p = {
            "image_path": str(image),
            "image_sha256": manifest["sha256"],
            "image_source_sha": manifest["source_sha"],
            **self.p["binding"],
        }
        original_stat = Path.stat

        def sealed_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            result = original_stat(path, *args, **kwargs)
            if path == image:
                fields = list(result)
                fields[4] = 0
                return os.stat_result(fields)
            return result

        with patch.object(Path, "stat", sealed_stat):
            with self.assertRaisesRegex(RuntimeError, "not an ordinary fleet image"):
                candidate.verify_image(p, self.root / "state")

    def test_document_backend_requires_its_explicit_manifest_and_every_input_binding(
        self,
    ) -> None:
        operation, image, manifest = dc.paths(self.p)
        operation.mkdir(parents=True)
        expected = {
            "operation_id": self.p["operation_id"],
            "source_sha": self.p["execution_source_sha"],
            "base_image_sha256": self.p["base_sha256"],
            "base_manifest_sha256": self.p["base_manifest_sha256"],
            "document_closure_sha256": self.p["document_closure_sha256"],
            "validation_sources": self.p["sources"],
            **self.p["binding"],
        }
        manifest.write_text(json.dumps(expected))
        state = Mock(directory=self.root / "state")
        backend = dc.DocumentBackend(state, self.p, validation=True)
        self.assertEqual(backend.image, image)
        self.assertEqual(backend.candidate_manifest, manifest)
        self.assertIs(backend.document_overlay, True)
        for key in expected:
            manifest.write_text(json.dumps(expected | {key: None}))
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(RuntimeError, "differs from this operation"),
            ):
                dc.DocumentBackend(state, self.p, validation=True)

    def test_actual_service_resume_rejects_overlay_before_unpausing_or_network(
        self,
    ) -> None:
        image = self.root / "images/qualified.qcow2"
        image.write_bytes(b"synthetic overlay")
        manifest = self.overlay_manifest(image)
        (self.root / "images/qualified.json").write_text(json.dumps(manifest))
        self.seed_state()
        state = State(self.root / "state")
        state.set(
            "preflight",
            {
                "status": "passed",
                "observed_at": time.time(),
                "checks": [{"check": "image", "status": "passed", "detail": manifest}],
            },
        )
        try:
            with (
                patch.object(service, "State", return_value=state),
                patch.object(
                    service, "observed", side_effect=AssertionError("must not resume")
                ),
                patch.object(
                    service,
                    "binding",
                    side_effect=AssertionError("overlay rejected first"),
                ),
            ):
                result = cast(Callable[..., dict[str, Any]], service.control)(
                    {}, self.root / "state", "resume"
                )
            self.assertEqual(result["status"], "blocked")
            self.assertIn("Document-only overlay", result["reason"])
            self.assertIs(state.get("drained"), True)
            self.assertEqual(
                json.loads((self.root / "images/qualified.json").read_text()), manifest
            )
        finally:
            state.db.close()


class ValidationScriptTests(Fixture):
    def test_all_sources_are_exact_and_file_only_without_registration_or_fetch(
        self,
    ) -> None:
        script = dc.validation_script(self.p)
        self.assertIn("GIT_ALLOW_PROTOCOL=file", script)
        self.assertIn("CARGO_NET_OFFLINE=true", script)
        self.assertIn("PIP_NO_INDEX=1", script)
        self.assertEqual(script.count("clone --no-checkout --no-local"), 10)
        self.assertEqual(script.count("checkout --detach"), 10)
        self.assertEqual(script.count("sha256sum --check --strict"), 10)
        self.assertEqual(script.count("remote remove origin"), 10)
        self.assertEqual(script.count("git ls-tree HEAD components/"), 7)
        self.assertIn(
            "/opt/runspindle-documents/bin/python tools/check_document_runtime.py",
            script,
        )
        self.assertIn("/opt/runspindle-documents/bin/python tools/validate.py", script)
        self.assertIn(
            "/opt/runspindle-documents/bin/python -O tools/validate.py", script
        )
        self.assertEqual(
            script.splitlines()[-1], "echo RUNSPINDLE_DOCUMENT_VALIDATION=passed"
        )
        for forbidden in (
            "git fetch",
            "git pull",
            "pip install",
            "curl ",
            "apt-get",
            "config.sh",
            "./run.sh",
            "gh ",
        ):
            self.assertNotIn(forbidden, script)


if __name__ == "__main__":
    unittest.main()
