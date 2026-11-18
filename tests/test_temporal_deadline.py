"""Synthetic supervisor ownership/cleanup tests; no Docker calls."""

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools import temporal_deadline as d


class DeadlineTests(unittest.TestCase):
    def test_names_require_exact_run_and_kind(self) -> None:
        prefix = "runspindle-temporal-" + "a" * 32 + "-" + "b" * 32
        self.assertTrue(d.owns(prefix + "-server", "a" * 32, "container"))
        self.assertTrue(d.owns(prefix + "-client-" + "c" * 12, "a" * 32, "container"))
        self.assertFalse(d.owns(prefix + "-server", "c" * 32, "container"))
        self.assertFalse(d.owns(prefix + "-db", "a" * 32, "container"))
        with self.assertRaises(ValueError):
            d.owns(prefix, "invalid", "container")

    def test_labels_are_rechecked_before_cleanup(self) -> None:
        name = "runspindle-temporal-" + "a" * 32 + "-" + "b" * 32 + "-net"
        record = {
            "Id": "f" * 64,
            "Name": name,
            "Labels": {"runspindle.synthetic": "false"},
        }
        with patch.object(
            d, "docker", side_effect=["ffff " + name, json.dumps([record])]
        ):
            with self.assertRaises(ValueError):
                d.inventory("network", "a" * 32, time.monotonic() + 10)

    def test_cleanup_orders_containers_before_data_network(self) -> None:
        calls = []

        def rows(kind: str, run_id: str, deadline: float) -> list[tuple[str, str]]:
            calls.append(kind)
            return []

        with patch.object(d, "inventory", side_effect=rows):
            d.cleanup("a" * 32)
        self.assertEqual(
            calls, ["container", "container", "volume", "volume", "network", "network"]
        )

    def test_process_stop_failure_does_not_skip_owned_cleanup(self) -> None:
        child = Mock(pid=123)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "evidence"
            with (
                patch.object(d, "inventory", return_value=[]),
                patch.object(subprocess, "Popen", return_value=child),
                patch.object(d, "capture", side_effect=InterruptedError),
                patch.object(d, "stop_group", side_effect=ProcessLookupError),
                patch.object(d, "cleanup") as cleanup,
            ):
                self.assertEqual(d.run(Path("/synthetic/prepared"), out), 1)
            cleanup.assert_called_once()
            receipt = json.loads((out / "result.json").read_text())
            self.assertEqual(receipt["cleanup"], "uncertain_process")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["process_stop_error_type"], "ProcessLookupError")


class RealProcessTests(unittest.TestCase):
    def test_exited_leader_does_not_leave_background_child(self) -> None:
        code = "import os,time; pid=os.fork(); print(pid,flush=True) if pid else time.sleep(30)"
        child = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output = io.BytesIO()
        try:
            self.assertEqual(d.capture(child, time.monotonic() + 3, 1024, output), 0)
            self.assertIsNone(child.returncode)  # Leader not reaped.
            self.assertTrue(d.active_group(child.pid))  # Sleeping descendant remains.
        finally:
            d.stop_group(child, grace=0.1)
            if child.stdout is not None:
                child.stdout.close()
        self.assertEqual(d.active_group(child.pid), [])

    def test_fast_overflow_is_rejected_without_oversized_output(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", "import os; os.write(1,b'x'*1000000)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output = io.BytesIO()
        try:
            with self.assertRaisesRegex(ValueError, "output bound"):
                d.capture(child, time.monotonic() + 3, 1024, output)
            self.assertLessEqual(len(output.getvalue()), 1024)
        finally:
            d.stop_group(child, grace=0.1)
            if child.stdout is not None:
                child.stdout.close()

    def test_workload_deadline_preserves_owned_termination(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            with self.assertRaises(TimeoutError):
                d.capture(child, time.monotonic() + 0.1, 1024, io.BytesIO())
        finally:
            d.stop_group(child, grace=0.1)
            if child.stdout is not None:
                child.stdout.close()
        self.assertEqual(d.active_group(child.pid), [])
