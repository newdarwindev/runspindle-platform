"""Synthetic host processes only; no fleet helper, image, guest or network work."""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.supervision import (
    Deadline,
    Interrupted,
    UncertainProcess,
    alarm,
    supervise,
    terminate,
    watch_descendants,
)


class SupervisionTests(unittest.TestCase):
    def test_real_pure_child_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            supervise(
                [sys.executable, "-I", "-B", "-c", "print('synthetic')"],
                "",
                root / "ok",
                time.monotonic() + 4,
            )
            self.assertEqual((root / "ok").read_text().strip(), "synthetic")
            with self.assertRaises(RuntimeError):
                supervise(
                    [sys.executable, "-I", "-B", "-c", "raise SystemExit(3)"],
                    "",
                    root / "bad",
                    time.monotonic() + 4,
                )

    def test_noncooperative_child_is_killed_within_phase_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            start = time.monotonic()
            script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            with self.assertRaises(subprocess.TimeoutExpired):
                supervise(
                    [sys.executable, "-I", "-B", "-c", script],
                    "",
                    Path(directory) / "hung",
                    start + 3.3,
                )
            self.assertLess(time.monotonic() - start, 3.3)

    def test_parent_cancellation_terminates_phase_before_return(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            start = time.monotonic()
            script = "import os,signal,time;os.kill(os.getppid(),signal.SIGTERM);time.sleep(60)"
            with self.assertRaises(Interrupted), alarm(start + 5):
                supervise(
                    [sys.executable, "-I", "-B", "-c", script],
                    "",
                    Path(directory) / "cancelled",
                    start + 5,
                )
            self.assertLess(time.monotonic() - start, 3)

    def test_unknown_termination_never_reports_success(self) -> None:
        child = Mock(pid=321)
        with (
            patch("runner_fleet.supervision.os.killpg", side_effect=PermissionError),
            self.assertRaises(UncertainProcess),
        ):
            terminate(child, time.monotonic() + 1)
        child.wait.assert_not_called()

    def test_nested_timer_cannot_extend_overall_deadline(self) -> None:
        start = time.monotonic()
        with self.assertRaises(Deadline), alarm(start + 0.15):
            with alarm(start + 20):
                time.sleep(2)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_cleanup_masks_repeated_cancel_but_not_deadline(self) -> None:
        with alarm(time.monotonic() + 2, cancellation=False):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        with (
            self.assertRaises(Deadline),
            alarm(time.monotonic() + 0.05, cancellation=False),
        ):
            time.sleep(1)

    def test_unrelated_timer_is_not_replaced(self) -> None:
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            with (
                self.assertRaisesRegex(RuntimeError, "Existing timer"),
                alarm(time.monotonic() + 1),
            ):
                self.fail("foreign timer accepted")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def test_no_new_process_without_termination_reserve(self) -> None:
        with patch("runner_fleet.supervision.subprocess.Popen") as spawn:
            with self.assertRaises(Deadline):
                supervise(["never-executed"], "", Path("unused"), time.monotonic() + 1)
        spawn.assert_not_called()

    def test_interrupted_spawn_is_unknown_not_safe_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "runner_fleet.supervision.subprocess.Popen",
                side_effect=Interrupted("lost spawn response"),
            ):
                with self.assertRaises(UncertainProcess):
                    supervise(
                        ["never-executed"],
                        "",
                        Path(directory) / "unknown",
                        time.monotonic() + 4,
                    )

    def test_log_cap_does_not_limit_child_data_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = "import pathlib,resource,sys; p=pathlib.Path(sys.argv[1]); f=p.open('wb'); f.seek(65*1024**2); f.write(b'x'); f.close(); print(resource.getrlimit(resource.RLIMIT_FSIZE))"
            supervise(
                [sys.executable, "-I", "-B", "-c", script, str(root / "data")],
                "",
                root / "log",
                time.monotonic() + 4,
            )
            self.assertEqual((root / "data").stat().st_size, 65 * 1024**2 + 1)
            self.assertEqual(
                (root / "log").read_text().strip(),
                str(resource.getrlimit(resource.RLIMIT_FSIZE)),
            )

    def test_excess_log_terminates_without_growing_sink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            with patch("runner_fleet.supervision.LOG_LIMIT", 1024):
                with self.assertRaisesRegex(RuntimeError, "log limit"):
                    supervise(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-c",
                            "import sys,time;print('x'*4096,flush=True);time.sleep(60)",
                        ],
                        "",
                        log,
                        time.monotonic() + 4,
                    )
            self.assertLessEqual(log.stat().st_size, 1024)

    def test_detached_descendant_is_uncertain_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "log"
            script = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(1)'],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid,flush=True)"
            with self.assertRaisesRegex(UncertainProcess, "Detached"):
                supervise(
                    [sys.executable, "-I", "-B", "-c", script],
                    "",
                    log,
                    time.monotonic() + 4,
                )
            pid = int(log.read_text().strip())
            # This bounded, harmless synthetic sleeper exits by itself. Reap the
            # adopted test child, not arbitrary target/host descendants.
            until = time.monotonic() + 2
            while os.waitpid(pid, os.WNOHANG)[0] == 0:
                self.assertLess(time.monotonic(), until)
                time.sleep(0.02)

    def test_interrupted_descendant_observation_is_uncertain(self) -> None:
        with patch(
            "runner_fleet.supervision.os.waitid",
            side_effect=[ChildProcessError, Deadline("observation cap")],
        ):
            with (
                self.assertRaises(UncertainProcess),
                watch_descendants(time.monotonic() + 4),
            ):
                pass

    def test_descendant_observation_masks_soft_cancellation(self) -> None:
        calls = 0

        def observed(*args: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
                os.kill(os.getpid(), signal.SIGTERM)
            raise ChildProcessError

        with (
            patch("runner_fleet.supervision.os.waitid", side_effect=observed),
            watch_descendants(time.monotonic() + 4),
        ):
            pass
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
