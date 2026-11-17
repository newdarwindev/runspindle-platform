"""Synthetic lifecycle and Temporal receipt tests; no real Temporal test bodies."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import types
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, Mock, patch

from tools import hygiene
from tools import python_tests as launcher

ROOT = Path(__file__).resolve().parents[1]


def marker() -> bytes:
    evidence = {
        "mode": "required",
        "status": "passed",
        "started": list(launcher.REQUIRED_IDS),
        "passed": list(launcher.REQUIRED_IDS),
        "invocation": "fresh",
    }
    return b"TEMPORAL_COVERAGE=" + json.dumps(evidence).encode() + b"\n"


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.events: list[str] = []
        self.exit_code: int | None = 0
        self.wait_error: BaseException | None = None
        self.kill_error: OSError | None = None
        self.handler = signal.SIG_DFL
        self.change_handler_at_eof = False
        self.reads: list[bytes | BaseException] = [b""]
        self.repeat_chunk: bytes | None = None
        self.process: Any = Mock(pid=424242, returncode=None)
        self.process.stdout.fileno.return_value = 42
        self.process.poll.side_effect = self.poll
        self.process.wait.side_effect = self.wait
        self.selector = MagicMock()
        self.selector.__enter__.return_value = self.selector
        self.selector.select.side_effect = self.select
        patches = [
            patch("tools.hygiene.subprocess.Popen", return_value=self.process),
            patch("tools.hygiene.shutil.which", return_value="synthetic-python"),
            patch("tools.hygiene.signal.getsignal", side_effect=lambda _: self.handler),
            patch(
                "tools.hygiene.selectors.DefaultSelector", return_value=self.selector
            ),
            patch("tools.hygiene.os.set_blocking"),
            patch("tools.hygiene.os.read", side_effect=self.read),
            patch("tools.hygiene.os.killpg", side_effect=self.kill),
            patch("tools.hygiene.time.monotonic", side_effect=lambda: self.now),
            patch("tools.hygiene.time.sleep", side_effect=self.advance),
            patch(
                "tools.hygiene.uuid.uuid4",
                return_value=types.SimpleNamespace(hex="fresh"),
            ),
        ]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def advance(self, delay: float) -> None:
        self.assertGreaterEqual(delay, 0)
        self.now += delay

    def select(self, delay: float) -> list[bool]:
        self.advance(delay)
        return [True]

    def poll(self) -> int | None:
        self.events.append("poll")
        if self.process.returncode is None:
            self.process.returncode = self.exit_code
        return cast(int | None, self.process.returncode)

    def wait(self, *, timeout: float) -> int:
        self.events.append("wait")
        self.assertEqual(timeout, hygiene.PROCESS_CLEANUP_SECONDS)
        if self.wait_error is not None:
            raise self.wait_error
        self.process.returncode = -signal.SIGKILL
        return -signal.SIGKILL

    def kill(self, pid: int, sig: int) -> None:
        self.events.append("kill")
        self.assertEqual((pid, sig), (424242, signal.SIGKILL))
        self.assertIsNone(self.process.returncode, "post-reap group signal")
        if self.kill_error is not None:
            raise self.kill_error

    def read(self, fd: int, size: int) -> bytes:
        self.assertEqual((fd, size), (42, 8192))
        item = self.reads.pop(0) if self.reads else self.repeat_chunk
        if item is None:
            raise BlockingIOError()
        if isinstance(item, BaseException):
            raise item
        if not item and self.change_handler_at_eof:
            self.handler = signal.SIG_IGN
        return item

    def output(self, data: bytes, ending: bytes | BaseException | None = b"") -> None:
        self.reads = [data[n : n + 8192] for n in range(0, len(data), 8192)]
        if ending is not None:
            self.reads.append(ending)

    def run_entry(
        self,
        *,
        temporal: bool = False,
        timeout: float = 0.1,
        command: list[str] | None = None,
    ) -> dict[str, Any]:
        return hygiene.run_entry(
            ROOT,
            {
                "id": "python-tests" if temporal else "synthetic",
                "command": command
                or (launcher.CANONICAL_COMMAND if temporal else ["synthetic-python"]),
                "timeout_seconds": timeout,
            },
        )

    def test_drained_success_gets_no_signal_or_wait(self) -> None:
        receipt = self.run_entry()
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(receipt["output_complete"])
        self.assertNotIn("kill", self.events)
        self.assertNotIn("wait", self.events)
        self.process.stdout.close.assert_called_once()
        self.assertEqual(self.mocks[0].call_args.kwargs["bufsize"], 0)
        self.assertTrue(self.mocks[0].call_args.kwargs["start_new_session"])
        self.mocks[4].assert_called_once_with(42, False)

    def test_successful_leader_with_open_pipe_fails_without_signal(self) -> None:
        self.reads = []
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "output_incomplete")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["cleanup"], "unknown")
        self.assertFalse(receipt["output_complete"])
        self.assertNotIn("kill", self.events)
        self.assertNotIn("wait", self.events)
        self.process.stdout.close.assert_called_once()
        self.assertLessEqual(self.now, 2.1)
        self.assertIn(
            "reason=output_incomplete",
            hygiene.failure_summary({"checks": [receipt]})[0],
        )

    def test_failed_leader_preserves_exit_and_diagnostic_with_open_pipe(self) -> None:
        self.exit_code = 7
        self.output(b"AssertionError: PRIVATE_CANARY\n", ending=None)
        receipt = self.run_entry()
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(
            receipt["reported_diagnostic"]["category"], "assertion_failure"
        )
        self.assertNotIn("PRIVATE_CANARY", json.dumps(receipt))
        self.assertNotIn("kill", self.events)

    def test_timeout_signals_before_one_bounded_wait(self) -> None:
        self.exit_code = None
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "timeout")
        self.assertNotIn("exit_code", receipt)
        self.assertTrue(receipt["output_complete"])
        self.assertEqual(self.events.count("kill"), 1)
        self.assertEqual(self.events.count("wait"), 1)
        self.assertLess(self.events.index("kill"), self.events.index("wait"))

    def test_timeout_signal_and_reap_errors_are_finite_and_never_retried(self) -> None:
        self.exit_code = None
        self.kill_error = PermissionError("PRIVATE_CANARY")
        self.wait_error = subprocess.TimeoutExpired("PRIVATE_CANARY", 2)
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "timeout")
        self.assertEqual(receipt["signal_error"], "permission_denied")
        self.assertEqual(receipt["reap_error"], "timeout")
        self.assertEqual(receipt["cleanup"], "unknown")
        self.assertEqual(self.events.count("kill"), 1)
        self.assertEqual(self.events.count("wait"), 1)
        self.assertNotIn("PRIVATE_CANARY", json.dumps(receipt))

    def test_missing_group_preserves_timeout(self) -> None:
        self.exit_code = None
        self.kill_error = ProcessLookupError("PRIVATE_CANARY")
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "timeout")
        self.assertEqual(receipt["signal_error"], "not_found")
        self.assertEqual(receipt["cleanup"], "unknown")
        self.assertEqual(self.events.count("kill"), 1)

    def test_reap_os_error_is_not_process_start_failure(self) -> None:
        self.exit_code = None
        self.wait_error = ChildProcessError("PRIVATE_CANARY")
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "timeout")
        self.assertEqual(receipt["reap_error"], "not_child")

    def test_read_failure_after_reap_never_signals_and_keeps_exit(self) -> None:
        self.exit_code = 7
        self.reads = [OSError("PRIVATE_CANARY")]
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "output_read_failed")
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(receipt["output_read_error"], "os_error")
        self.assertNotIn("kill", self.events)
        self.assertNotIn("PRIVATE_CANARY", json.dumps(receipt))

    def test_read_failure_before_reap_stops_owned_leader(self) -> None:
        self.exit_code = None
        self.reads = [OSError()]
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "output_read_failed")
        self.assertEqual(self.events.count("kill"), 1)
        self.process.stdout.close.assert_called_once()

    def test_timeout_survives_later_read_and_close_failure(self) -> None:
        self.exit_code = None
        self.reads = [b"partial\n", OSError()]
        self.process.stdout.close.side_effect = PermissionError("PRIVATE_CANARY")
        receipt = self.run_entry(timeout=0.01)
        self.assertEqual(receipt["reason"], "timeout")
        self.assertEqual(receipt["output_read_error"], "os_error")
        self.assertEqual(receipt["output_close_error"], "permission_denied")
        self.assertEqual(receipt["cleanup"], "unknown")
        self.assertNotIn("PRIVATE_CANARY", json.dumps(receipt))

    def test_cancellation_keeps_primary_exception_through_cleanup(self) -> None:
        self.exit_code = None
        original = KeyboardInterrupt("primary")
        self.reads = [original]
        self.wait_error = KeyboardInterrupt("cleanup")
        self.process.stdout.close.side_effect = KeyboardInterrupt("close")
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_entry()
        self.assertIs(caught.exception, original)
        self.assertEqual(self.events.count("kill"), 1)
        self.assertEqual(self.events.count("wait"), 1)
        self.process.stdout.close.assert_called_once()

    def test_first_cancellation_during_timeout_cleanup_propagates(self) -> None:
        self.exit_code = None
        original = KeyboardInterrupt("cleanup")
        self.wait_error = original
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_entry()
        self.assertIs(caught.exception, original)
        self.assertEqual(self.events.count("kill"), 1)
        self.process.stdout.close.assert_called_once()

    def test_first_close_cancellation_ignores_ambient_exception_context(self) -> None:
        original = KeyboardInterrupt("close")
        self.process.stdout.close.side_effect = original
        try:
            raise ValueError("already handled by caller")
        except ValueError:
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_entry()
        self.assertIs(caught.exception, original)
        self.assertNotIn("kill", self.events)

    def test_first_cleanup_cancellation_after_read_error_propagates(self) -> None:
        self.exit_code = None
        self.reads = [OSError()]
        original = KeyboardInterrupt("cleanup")
        self.wait_error = original
        try:
            raise ValueError("already handled by caller")
        except ValueError:
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_entry()
        self.assertIs(caught.exception, original)
        self.assertEqual(self.events.count("kill"), 1)
        self.process.stdout.close.assert_called_once()

    def test_cancellation_after_reap_never_signals(self) -> None:
        self.reads = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_entry()
        self.assertNotIn("kill", self.events)
        self.process.stdout.close.assert_called_once()

    def test_nondefault_sigchld_rejected_before_launch(self) -> None:
        self.handler = signal.SIG_IGN
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "process_ownership_unavailable")
        self.mocks[0].assert_not_called()
        self.assertNotIn("kill", self.events)

    def test_changed_sigchld_prevents_stop_without_installing_handler(self) -> None:
        self.handler = signal.SIG_IGN
        with patch.object(signal, "signal") as install:
            result = hygiene.stop_owned_process(self.process)
        self.assertEqual(result["signal_error"], "ownership_unavailable")
        install.assert_not_called()
        self.assertNotIn("kill", self.events)
        self.assertNotIn("wait", self.events)

    def test_already_reaped_stop_never_signals_or_waits(self) -> None:
        self.process.returncode = 0
        self.assertEqual(hygiene.stop_owned_process(self.process), {})
        self.assertEqual(self.events, [])

    def test_continuous_output_is_bounded_after_leader_exit(self) -> None:
        self.reads = []
        self.repeat_chunk = b"x" * 8192
        outcome, capture = hygiene.collect_process(self.process, 0.1)
        self.assertEqual(outcome["reason"], "output_incomplete")
        self.assertLessEqual(self.now, 2.1)
        self.assertEqual(len(capture.tail), 65536)
        self.assertLessEqual(len(capture.line), 8192)
        self.assertNotIn("kill", self.events)

    def test_continuous_output_cannot_starve_timeout(self) -> None:
        self.exit_code = None
        self.reads = []
        self.repeat_chunk = b"x" * 8192
        receipt = self.run_entry()
        self.assertEqual(receipt["reason"], "timeout")
        self.assertFalse(receipt["output_complete"])
        self.assertLessEqual(self.now, 2.2)
        self.assertEqual(self.events.count("kill"), 1)

    def test_fresh_canonical_coverage_needs_successful_eof(self) -> None:
        self.output(marker())
        with patch.dict(os.environ, {launcher.OPT_IN: "/ambient"}):
            receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(launcher.valid_coverage(receipt["temporal_coverage"]))
        self.assertNotIn(launcher.OPT_IN, self.mocks[0].call_args.kwargs["env"])
        self.assertEqual(
            self.mocks[0].call_args.kwargs["env"]["RUNSPINDLE_TEST_INVOCATION"], "fresh"
        )

    def test_marker_before_tail_truncation_still_counts(self) -> None:
        self.output(marker() + b"x" * 70000 + b"\n")
        receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["status"], "passed")
        self.assertIn("temporal_coverage", receipt)

    def test_whole_stream_duplicate_marker_fails(self) -> None:
        self.output(marker() + b"x" * 70000 + b"\n" + marker())
        receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["reason"], "required_temporal_coverage_missing")
        self.assertNotIn("temporal_coverage", receipt)

    def test_invalid_markers_and_wrong_command_fail(self) -> None:
        cases = [
            (b"", launcher.CANONICAL_COMMAND),
            (marker() * 2, launcher.CANONICAL_COMMAND),
            (marker().replace(b"fresh", b"stale"), launcher.CANONICAL_COMMAND),
            (
                marker().replace(b'{"mode":', b'{"mode": "boundary", "mode":'),
                launcher.CANONICAL_COMMAND,
            ),
            (
                b"TEMPORAL_COVERAGE=" + b"x" * 9000 + b"\n",
                launcher.CANONICAL_COMMAND,
            ),
            (b"TEMPORAL_COVERAGE=invalid\n", launcher.CANONICAL_COMMAND),
            (
                b"TEMPORAL_COVERAGE=" + b"[" * 2000 + b"]" * 2000 + b"\n",
                launcher.CANONICAL_COMMAND,
            ),
            (marker(), ["python3", "tools/python_tests.py", "--temporal", "boundary"]),
        ]
        for data, command in cases:
            with self.subTest(data_length=len(data), command=command):
                self.output(data)
                receipt = self.run_entry(temporal=True, command=command)
                self.assertEqual(receipt["status"], "failed")
                self.assertNotIn("temporal_coverage", receipt)

    def test_nonzero_exit_discards_previously_emitted_success(self) -> None:
        self.exit_code = 7
        self.output(marker())
        receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(receipt["status"], "failed")
        self.assertNotIn("temporal_coverage", receipt)

    def test_timeout_discards_previously_emitted_success(self) -> None:
        self.exit_code = None
        self.output(marker())
        receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["reason"], "timeout")
        self.assertNotIn("temporal_coverage", receipt)

    def test_open_pipe_discards_previously_emitted_success_without_signal(self) -> None:
        self.output(marker(), ending=None)
        receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["reason"], "output_incomplete")
        self.assertNotIn("temporal_coverage", receipt)
        self.assertNotIn("kill", self.events)

    def test_read_failure_discards_previously_emitted_success(self) -> None:
        self.output(marker(), ending=OSError())
        receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["reason"], "output_read_failed")
        self.assertNotIn("temporal_coverage", receipt)
        self.assertNotIn("kill", self.events)

    def test_close_failure_rejects_eof_and_zero_exit_coverage(self) -> None:
        self.output(marker())
        self.process.stdout.close.side_effect = OSError()
        receipt = self.run_entry(temporal=True)
        self.assertTrue(receipt["output_complete"])
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["reason"], "output_close_failed")
        self.assertNotIn("temporal_coverage", receipt)
        self.assertNotIn("kill", self.events)

    def test_cancellation_after_complete_marker_never_returns_coverage(self) -> None:
        original = KeyboardInterrupt("after marker")
        self.output(marker(), ending=original)
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_entry(temporal=True)
        self.assertIs(caught.exception, original)
        self.assertNotIn("kill", self.events)
        self.process.stdout.close.assert_called_once()

    def test_close_cancellation_after_marker_and_eof_never_returns_coverage(
        self,
    ) -> None:
        original = KeyboardInterrupt("after EOF")
        self.output(marker())
        self.process.stdout.close.side_effect = original
        with self.assertRaises(KeyboardInterrupt) as caught:
            self.run_entry(temporal=True)
        self.assertIs(caught.exception, original)
        self.assertNotIn("kill", self.events)
        self.process.stdout.close.assert_called_once()

    def test_ownership_change_after_timeout_keeps_primary_reason(self) -> None:
        self.exit_code = None
        self.reads = []

        def wait_and_change(*, timeout: float) -> int:
            code = self.wait(timeout=timeout)
            self.handler = signal.SIG_IGN
            return code

        self.process.wait.side_effect = wait_and_change
        receipt = self.run_entry(temporal=True)
        self.assertEqual(receipt["reason"], "timeout")
        self.assertEqual(receipt["cleanup"], "unknown")
        self.assertNotIn("exit_code", receipt)
        self.assertNotIn("temporal_coverage", receipt)
        self.assertEqual(self.events.count("kill"), 1)

    def test_changed_ownership_rejects_eof_and_zero_exit_coverage(self) -> None:
        self.output(marker())
        self.change_handler_at_eof = True
        receipt = self.run_entry(temporal=True)
        self.assertTrue(receipt["output_complete"])
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["reason"], "process_ownership_unavailable")
        self.assertNotIn("temporal_coverage", receipt)
        self.assertNotIn("kill", self.events)


class CaptureAndPipeTests(unittest.TestCase):
    def test_chunk_boundaries_and_final_unterminated_marker(self) -> None:
        data = b"other\n" + marker().rstrip(b"\n")
        for width in (1, 7, 8192):
            capture = hygiene.OutputCapture()
            for start in range(0, len(data), width):
                capture.feed(data[start : start + width])
            self.assertEqual(capture.coverage_count, 0)
            capture.finish_line()
            self.assertEqual(capture.coverage_count, 1)
            self.assertEqual(capture.coverage_line, marker().rstrip(b"\n"))

    def test_overlong_marker_and_later_valid_marker_remain_duplicates(self) -> None:
        capture = hygiene.OutputCapture()
        for _ in range(3):
            capture.feed(b"TEMPORAL_COVERAGE=" + b"x" * 9000 + b"\n")
        capture.feed(marker())
        self.assertEqual(capture.coverage_count, 2)
        self.assertIsNone(capture.coverage_line)
        self.assertLessEqual(len(capture.tail), 65536)
        self.assertLessEqual(len(capture.line), 8192)

    def test_real_retained_pipe_is_closed_without_buffered_reader_lock(self) -> None:
        for code in (0, 7):
            read_fd, write_fd = os.pipe()
            stream = os.fdopen(read_fd, "rb", buffering=0)
            process: Any = Mock(pid=424242, returncode=None, stdout=stream)

            def reap() -> int:
                process.returncode = code
                return code

            process.poll.side_effect = reap
            started = time.monotonic()
            try:
                os.write(write_fd, b"synthetic output\n")
                with (
                    patch.object(hygiene, "PROCESS_CLEANUP_SECONDS", 0.03),
                    patch("tools.hygiene.os.killpg") as kill,
                ):
                    outcome, _ = hygiene.collect_process(process, 0.1)
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(outcome["exit_code"], code)
                self.assertEqual(outcome["reason"], "output_incomplete")
                self.assertTrue(stream.closed)
                with self.assertRaises(OSError):
                    os.fstat(read_fd)
                kill.assert_not_called()
                process.wait.assert_not_called()
            finally:
                stream.close()
                os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
