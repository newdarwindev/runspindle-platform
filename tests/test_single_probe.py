"""No real guests/network; actual shared reservation database and fake backend."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from tools.runner_fleet import single_probe as probe
from tools.runner_fleet.state import State


def intended() -> dict[str, Any]:
    return {
        "bindings": dict.fromkeys(probe.FIELDS, "a" * 64),
        "image": "/synthetic.qcow2",
        "manifest": {"original": "retained"},
        "rust_closure_sha256": "r",
        "builder_archive_sha256": "b",
        "quality_closure_sha256": "q",
    }


def observation(resource: dict[str, Any]) -> dict[str, Any]:
    p = intended()
    return {
        "instance": p["bindings"]
        | {"resource": resource["id"], "slot": resource["slot"]},
        "rust_closure_sha256": "r",
        "builder_archive_sha256": "b",
        "quality_closure_sha256": "q",
        "versions": {
            k: {"code": 0, "version": v}
            for k, v in [
                ("cargo", "cargo 1.94.1"),
                ("rustc", "rustc 1.94.1"),
                ("ruff", "ruff 0.14.5"),
            ]
        },
    }


class Tests(unittest.TestCase):
    def test_wrong_binding_closure_version_denied(self) -> None:
        r = {"id": "owned", "slot": 0}
        for field in probe.FIELDS:
            o = observation(r)
            o["instance"][field] = "wrong"
            with self.assertRaises(ValueError):
                probe.validate_observation(o, intended(), r)
        for field in (
            "rust_closure_sha256",
            "builder_archive_sha256",
            "quality_closure_sha256",
        ):
            o = observation(r)
            o[field] = "wrong"
            with self.assertRaises(ValueError):
                probe.validate_observation(o, intended(), r)
        o = observation(r)
        o["versions"]["ruff"]["version"] = "ruff wrong"
        with self.assertRaises(ValueError):
            probe.validate_observation(o, intended(), r)
        probe.validate_observation(observation(r), intended(), r)

    def test_one_guest_no_registration_and_owned_cleanup(self) -> None:
        for fail in (None, "create", "observe", "destroy", "sigterm"):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                state = State(Path(tmp))
                self.addCleanup(state.db.close)
                original = Path(tmp) / "candidate.json"
                original.write_text("untouched")
                calls: list[str] = []

                class Backend:
                    def __init__(self, shared: Any, image: Any) -> None:
                        self.state = shared

                    def create(self, r: dict[str, Any]) -> None:
                        calls.append("create")
                        self.state.update(r, "starting", image_sha256="a" * 64)
                        if fail == "create":
                            raise RuntimeError("failure")

                    def ready(self, r: dict[str, Any]) -> bool:
                        return True

                    def ssh(self, r: dict[str, Any], script: str, timeout: int) -> Any:
                        calls.append("observe")
                        if fail == "observe":
                            raise KeyboardInterrupt()
                        if fail == "sigterm":
                            os.kill(os.getpid(), signal.SIGTERM)
                        return SimpleNamespace(stdout=json.dumps(observation(r)))

                    def destroy(self, r: dict[str, Any]) -> None:
                        calls.append("destroy")
                        if fail == "destroy":
                            raise RuntimeError("failed stop")

                    def register(self, *args: Any) -> None:
                        raise AssertionError("forbidden")

                with (
                    patch.object(probe, "STATE_DIRECTORY", Path(tmp)),
                    patch.object(probe, "manager_stopped"),
                    patch.object(probe, "verify_guest_lifetime"),
                    patch.object(probe, "Backend", Backend),
                    patch.object(
                        probe, "helper", return_value={"ActiveState": "inactive"}
                    ),
                ):
                    previous = signal.signal(signal.SIGTERM, probe.terminated)
                    try:
                        result = probe.run(intended(), state)
                    finally:
                        signal.signal(signal.SIGTERM, previous)
                self.assertEqual(calls.count("create"), 1)
                self.assertEqual(calls.count("destroy"), 1)
                self.assertEqual(original.read_text(), "untouched")
                self.assertEqual(len(state.active()), 1 if fail == "destroy" else 0)
                self.assertEqual(
                    result["status"], "passed" if fail is None else "failed"
                )
                self.assertFalse(result["registered"])
                self.assertFalse(result["ci_qualified"])

    def test_existing_reservation_prevents_any_backend_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp))
            self.addCleanup(state.db.close)
            state.reserve({"repository": "runspindle-core", "id": 123})
            with (
                patch.object(probe, "STATE_DIRECTORY", Path(tmp)),
                patch.object(probe, "manager_stopped"),
                patch.object(probe, "Backend") as backend,
            ):
                with self.assertRaises(ValueError):
                    probe.run(intended(), state)
                backend.assert_not_called()
            self.assertEqual(len(state.active()), 1)

    def test_database_failure_does_not_skip_owned_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp))
            self.addCleanup(state.db.close)
            resource = state.reserve({"repository": "runspindle-platform", "id": -1})
            backend = Mock()
            report: dict[str, Any] = {"status": "passed"}
            with (
                patch.object(
                    state, "update", side_effect=RuntimeError("DB unavailable")
                ),
                patch.object(probe, "helper", return_value={"ActiveState": "inactive"}),
            ):
                probe.cleanup(state, backend, resource, report)
            backend.destroy.assert_called_once_with(resource)
            self.assertEqual(len(state.active()), 1)
            self.assertEqual(report["cleanup"], "blocked")

    def test_alternate_state_and_nonregular_manifest_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = State(root)
            self.addCleanup(state.db.close)
            with self.assertRaises(ValueError):
                probe.validate_state_directory(root)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                probe.read_record(fifo)
            regular = root / "record.json"
            regular.write_text("{}")
            link = root / "link"
            link.symlink_to(regular)
            with self.assertRaises(OSError):
                probe.read_record(link)

    def test_manager_refuses_probe_default_image_cleanup(self) -> None:
        from tools.runner_fleet.manager import Manager

        manager = Manager.__new__(Manager)
        manager.state = Mock()
        manager.backend = Mock()
        resource = {"details": {"unregistered_probe": True}}
        manager.cleanup(resource, "restart")
        manager.state.update.assert_called_once()
        manager.backend.destroy.assert_not_called()

    def test_explicit_recovery_uses_persisted_owned_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = State(Path(tmp))
            self.addCleanup(state.db.close)
            directory = Path(tmp) / ("single-probe-" + "a" * 32)
            directory.mkdir(mode=0o700)
            (directory / "candidate.json").write_text(
                json.dumps(intended()["manifest"])
            )
            resource = state.reserve(
                {"repository": "runspindle-platform", "id": -1},
                details={
                    "unregistered_probe": True,
                    "probe_directory": directory.name,
                    "probe_image": intended()["image"],
                    "image_sha256": "a" * 64,
                },
            )
            backend = Mock()
            with (
                patch.object(probe, "STATE_DIRECTORY", Path(tmp)),
                patch.object(probe, "manager_stopped"),
                patch.object(probe, "Backend", return_value=backend) as factory,
                patch.object(probe, "helper", return_value={"ActiveState": "inactive"}),
            ):
                result = probe.recover(intended(), state, resource["id"])
            self.assertEqual(factory.call_args.args[0].directory, directory)
            backend.create.assert_not_called()
            backend.register.assert_not_called()
            backend.destroy.assert_called_once()
            self.assertEqual(state.active(), [])
            self.assertTrue(result["recovery_only"])

    def test_deployed_guard_drift_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manager.py"
            path.write_text("old controller")
            with patch.object(probe, "DEPLOYED_MANAGER", path):
                with self.assertRaises(ValueError):
                    probe.verify_deployed_guard()
                path.write_bytes(
                    Path(probe.__file__).with_name("manager.py").read_bytes()
                )
                with patch.object(
                    probe,
                    "execute",
                    return_value=SimpleNamespace(
                        stdout="{ argv[]=/usr/bin/python3 /opt/runspindle-fleet/tools/fleet.py manage --host strix --state-dir /home/standard/.local/state/runspindle-fleet ; }"
                    ),
                ):
                    probe.verify_deployed_guard()

    def test_cli_guard_failure_has_zero_state_or_backend_effects(self) -> None:
        argv = [
            "single_probe",
            "--image",
            "image",
            "--manifest",
            "manifest",
            "--expected",
            "expected",
            "--state-dir",
            str(probe.STATE_DIRECTORY),
            "--execute",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(probe, "plan", return_value=intended()),
            patch.object(probe, "validate_state_directory"),
            patch.object(
                probe, "verify_deployed_guard", side_effect=ValueError("guard mismatch")
            ),
            patch.object(probe, "State") as state,
            patch.object(probe, "Backend") as backend,
        ):
            with self.assertRaises(ValueError):
                probe.main()
            state.assert_not_called()
            backend.assert_not_called()

    def test_independent_host_lifetime_gate(self) -> None:
        good = "RuntimeMaxUSec=25min\nTimeoutStopUSec=20s\nKillMode=control-group\n"
        with patch.object(probe, "execute", return_value=SimpleNamespace(stdout=good)):
            probe.verify_guest_lifetime(0)
        for wrong in (
            good.replace("25min", "infinity"),
            good.replace("control-group", "process"),
        ):
            with patch.object(
                probe, "execute", return_value=SimpleNamespace(stdout=wrong)
            ):
                with self.assertRaises(ValueError):
                    probe.verify_guest_lifetime(0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "helper.py"
            path.write_text("old helper")
            with patch.object(probe, "DEPLOYED_HELPER", path):
                with self.assertRaises(ValueError):
                    probe.verify_host_lifetime_policy()
                path.write_bytes(
                    Path(probe.__file__).with_name("host_helper.py").read_bytes()
                )
                probe.verify_host_lifetime_policy()
