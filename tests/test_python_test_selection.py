"""Synthetic execution only: never load or execute the real Docker test bodies."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from tools import hygiene, temporal_lane
from tools import python_tests as launcher


class SelectionTests(unittest.TestCase):
    def invoke(
        self, mode: str, fault: str = ""
    ) -> tuple[int, dict[str, Any], list[str]]:
        effects: list[str] = []

        def body(test: unittest.TestCase) -> None:
            effects.append(test.id())
            if fault == "skip":
                test.skipTest("synthetic skip")
            if fault == "failure":
                test.fail("synthetic failure")

        real = type(
            "RealLaneTests",
            (unittest.TestCase,),
            {
                "__module__": "test_temporal_lane",
                **{
                    identity.rsplit(".", 1)[1]: body
                    for identity in launcher.REQUIRED_IDS
                },
            },
        )
        module = types.ModuleType("test_temporal_lane")
        module.RealLaneTests = real  # type: ignore[attr-defined]
        cases = list(unittest.defaultTestLoader.loadTestsFromTestCase(real))
        if fault == "missing":
            cases.pop()
        if fault == "duplicate":
            cases.append(cases[0])
        if fault == "substitution":
            impostor = unittest.FunctionTestCase(lambda: None)
            impostor.id = lambda: launcher.REQUIRED_IDS[0]  # type: ignore[method-assign]
            cases.append(impostor)
        with (
            patch.dict(sys.modules, {"test_temporal_lane": module}),
            patch.dict(os.environ, {launcher.OPT_IN: "/ambient/must/not/select"}),
            patch.object(launcher, "prepared_input") as preflight,
            patch.object(
                unittest.defaultTestLoader,
                "discover",
                return_value=unittest.TestSuite(cases),
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code, evidence = launcher.run(
                mode, Path("/explicit") if mode == "required" else None
            )
            self.assertNotIn(launcher.OPT_IN, os.environ)
            self.assertEqual(preflight.call_count, int(mode == "required"))
        return code, evidence, effects

    def test_boundary_ignores_ambient_opt_in_and_directory(self) -> None:
        with patch.object(Path, "is_dir", return_value=True):
            code, evidence, effects = self.invoke("boundary")
        self.assertEqual(code, 0)
        self.assertEqual(effects, [])
        self.assertEqual(evidence["status"], "not_run")
        self.assertFalse(launcher.valid_coverage(evidence))

    def test_required_observes_all_five_synthetic_bodies(self) -> None:
        code, evidence, effects = self.invoke("required")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(effects), list(launcher.REQUIRED_IDS))
        self.assertTrue(launcher.valid_coverage(evidence))

    def test_missing_duplicate_and_substitution_fail_before_effects(self) -> None:
        for fault in ("missing", "duplicate", "substitution"):
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.invoke("required", fault)

    def test_skip_and_failure_never_satisfy_coverage(self) -> None:
        for fault in ("skip", "failure"):
            with self.subTest(fault=fault):
                code, evidence, _ = self.invoke("required", fault)
                self.assertEqual(code, 1)
                self.assertFalse(launcher.valid_coverage(evidence))

    def test_missing_and_invalid_preparation_never_discover_or_call_docker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for prepared in (None, Path(directory) / "missing", Path(directory)):
                with (
                    self.subTest(prepared=prepared),
                    patch.object(unittest.defaultTestLoader, "discover") as discover,
                    patch.object(temporal_lane, "docker") as docker,
                    self.assertRaises((ValueError, OSError)),
                ):
                    launcher.run("required", prepared)
                discover.assert_not_called()
                docker.assert_not_called()

    def test_hygiene_requires_current_complete_envelope_and_canonical_command(
        self,
    ) -> None:
        _, good, _ = self.invoke("required")
        marker = (
            "TEMPORAL_COVERAGE=" + json.dumps(good | {"invocation": "fresh"}) + "\n"
        )
        for output, command, exit_code, passed in (
            (marker, launcher.CANONICAL_COMMAND, 0, True),
            ("", launcher.CANONICAL_COMMAND, 0, False),
            (marker * 2, launcher.CANONICAL_COMMAND, 0, False),
            (
                marker.replace('{"mode":', '{"mode": "boundary", "mode":'),
                launcher.CANONICAL_COMMAND,
                0,
                False,
            ),
            (
                marker + "x" * 70000 + "\n" + marker,
                launcher.CANONICAL_COMMAND,
                0,
                False,
            ),
            (
                "TEMPORAL_COVERAGE=" + "x" * 9000 + "\n",
                launcher.CANONICAL_COMMAND,
                0,
                False,
            ),
            (marker.replace("fresh", "stale"), launcher.CANONICAL_COMMAND, 0, False),
            ("TEMPORAL_COVERAGE=invalid\n", launcher.CANONICAL_COMMAND, 0, False),
            (marker, launcher.CANONICAL_COMMAND, 1, False),
            (
                marker,
                ["python3", "tools/python_tests.py", "--temporal", "boundary"],
                0,
                False,
            ),
        ):
            child = MagicMock()
            child.stdout = io.BytesIO(output.encode())
            child.wait.return_value = exit_code
            with (
                patch.object(subprocess, "Popen", return_value=child) as popen,
                patch.object(
                    uuid,
                    "uuid4",
                    return_value=types.SimpleNamespace(hex="fresh"),
                ),
                patch.object(shutil, "which", return_value="/synthetic/python3"),
                patch.dict(os.environ, {launcher.OPT_IN: "/ambient"}),
            ):
                receipt = hygiene.run_entry(
                    launcher.ROOT, {"id": "python-tests", "command": command}
                )
            self.assertEqual(receipt["status"] == "passed", passed)
            self.assertNotIn(launcher.OPT_IN, popen.call_args.kwargs["env"])

    def test_timeout_discards_previously_emitted_success(self) -> None:
        _, good, _ = self.invoke("required")
        child = MagicMock()
        child.stdout = io.BytesIO(
            (
                "TEMPORAL_COVERAGE=" + json.dumps(good | {"invocation": "fresh"}) + "\n"
            ).encode()
        )
        child.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 1), 0]
        with (
            patch.object(subprocess, "Popen", return_value=child),
            patch.object(shutil, "which", return_value="/synthetic/python3"),
            patch.object(
                uuid, "uuid4", return_value=types.SimpleNamespace(hex="fresh")
            ),
            patch.object(os, "killpg") as kill,
        ):
            receipt = hygiene.run_entry(
                launcher.ROOT,
                {"id": "python-tests", "command": launcher.CANONICAL_COMMAND},
            )
        kill.assert_called_once()
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["reason"], "timeout")
        self.assertNotIn("temporal_coverage", receipt)

    def test_incomplete_or_forced_drain_rejects_fresh_success(self) -> None:
        _, good, _ = self.invoke("required")
        marker = (
            "TEMPORAL_COVERAGE=" + json.dumps(good | {"invocation": "fresh"}) + "\n"
        ).encode()
        for fault in ("read_error", "still_alive", "forced_then_eof"):
            child = MagicMock()
            child.wait.return_value = 0
            child.stdout.read.side_effect = [
                marker,
                OSError("synthetic read failure") if fault == "read_error" else b"",
            ]

            def make_reader(*, target: Any, daemon: bool) -> Any:
                reader = MagicMock()
                if fault == "forced_then_eof":
                    # The second join observes EOF only after forced cleanup.
                    joins = 0

                    def join(*, timeout: int) -> None:
                        nonlocal joins
                        joins += 1
                        if joins == 2:
                            target()

                    reader.join.side_effect = join
                    reader.is_alive.side_effect = [True, False]
                else:
                    reader.start.side_effect = target
                    reader.is_alive.return_value = fault == "still_alive"
                return reader

            with (
                self.subTest(fault=fault),
                patch.object(threading, "Thread", side_effect=make_reader),
                patch.object(subprocess, "Popen", return_value=child),
                patch.object(shutil, "which", return_value="/synthetic/python3"),
                patch.object(
                    uuid, "uuid4", return_value=types.SimpleNamespace(hex="fresh")
                ),
                patch.object(os, "killpg") as kill,
            ):
                receipt = hygiene.run_entry(
                    launcher.ROOT,
                    {"id": "python-tests", "command": launcher.CANONICAL_COMMAND},
                )
            self.assertEqual(receipt["status"], "failed")
            self.assertNotIn("temporal_coverage", receipt)
            self.assertEqual(kill.call_count, int(fault != "read_error"))

    def test_receipt_rejects_forged_incomplete_or_wrong_mode(self) -> None:
        _, good, _ = self.invoke("required")
        for key, value in (
            ("mode", "boundary"),
            ("status", "skipped"),
            ("started", []),
            ("passed", list(launcher.REQUIRED_IDS)[:-1]),
            ("passed", list(launcher.REQUIRED_IDS) + [launcher.REQUIRED_IDS[0]]),
            ("passed", ["substitute"] * 5),
        ):
            changed = copy.deepcopy(good)
            changed[key] = value
            self.assertFalse(launcher.valid_coverage(changed))
        self.assertFalse(launcher.valid_coverage({"status": "passed"}))
        self.assertFalse(launcher.valid_coverage(good | {"extra": True}))


if __name__ == "__main__":
    unittest.main()
