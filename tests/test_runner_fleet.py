"""Synthetic unit evidence only; no VM or GitHub workload qualification."""

import json
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet.model import (
    config,
    choose_jobs,
    classify_job,
    empty_report,
    matching_lane,
)
from runner_fleet.evidence import collect
from runner_fleet.preflight import inspect_host
import fleet


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()

    def job(self, repo, lane="strix-linux-x64", identity=1):
        return {
            "id": identity,
            "repository": repo,
            "labels": self.cfg["lanes"][lane]["labels"],
        }

    def test_round_robin_and_shared_capacity(self):
        jobs = [
            self.job(repo, identity=i)
            for i, repo in enumerate(self.cfg["repositories"], start=1)
        ]
        cursor = 0
        seen = []
        for _ in range(4):
            selected, cursor = choose_jobs(self.cfg, "strix", jobs, [], cursor)
            self.assertEqual(len(selected), 2)
            seen.extend(j["repository"] for j in selected)
        self.assertEqual(seen, self.cfg["repositories"])
        self.assertEqual(choose_jobs(self.cfg, "strix", jobs, [{}, {}])[0], [])
        self.assertEqual(len(choose_jobs(self.cfg, "strix", jobs, [{}])[0]), 1)
        self.assertEqual(choose_jobs(self.cfg, "strix", jobs, [], drained=True)[0], [])

    def test_duplicate_and_already_reserved_jobs_cannot_reserve_again(self):
        job = self.job("runspindle", identity=123)
        self.assertEqual(
            len(choose_jobs(self.cfg, "strix", [job, dict(job)], [])[0]), 1
        )
        for state in ("booting", "unknown", "running"):
            self.assertEqual(
                choose_jobs(self.cfg, "strix", [job], [dict(job, state=state)])[0], []
            )
        other = self.job("runspindle-core", identity=123)
        self.assertEqual(choose_jobs(self.cfg, "strix", [other], [job])[0], [other])
        for identity in (None, True, 0, -1, "123"):
            self.assertEqual(
                choose_jobs(self.cfg, "strix", [dict(job, id=identity)], [])[0], []
            )

    def test_mac_linux_and_native_share_one_slot(self):
        jobs = [
            self.job("runspindle", lane)
            for lane in ("macbook-linux-arm64", "macbook-macos-arm64")
        ]
        self.assertEqual(len(choose_jobs(self.cfg, "macbook", jobs, [])[0]), 1)
        self.assertEqual(
            choose_jobs(self.cfg, "macbook", jobs, [{"state": "unknown"}])[0], []
        )
        self.assertEqual(choose_jobs(self.cfg, "strix", jobs, [])[0], [])

    def test_reject_generic_untrusted_or_incompatible_work(self):
        self.assertIsNone(
            matching_lane(self.cfg, "strix", ["self-hosted", "linux", "x64"])
        )
        self.assertIsNone(
            matching_lane(
                self.cfg,
                "strix",
                ["self-hosted", "runspindle-isolated", "strix", "ARM64"],
            )
        )
        self.assertEqual(
            choose_jobs(self.cfg, "strix", [self.job("attacker")], [])[0], []
        )
        self.assertIsNone(
            matching_lane(
                self.cfg,
                "strix",
                ["self-hosted", "runspindle-isolated", "strix", "gpu"],
            )
        )


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.sha = "a" * 40
        self.job = {
            "id": 3,
            "name": "strix-linux-x64",
            "status": "completed",
            "conclusion": "success",
            "head_sha": self.sha,
            "runner_name": "runspindle-fleet-test",
            "runner_id": 123,
            "labels": self.cfg["lanes"]["strix-linux-x64"]["labels"],
            "steps": [{"name": "Run implemented validation", "conclusion": "success"}],
        }

    def test_no_echo_or_wrong_runner_or_sha_can_qualify(self):
        lane = self.cfg["lanes"]["strix-linux-x64"]
        self.assertEqual(classify_job(self.job, self.sha, lane), ("passed", None))
        for change in (
            {"runner_name": "persistent"},
            {"runner_id": 0},
            {"head_sha": "b" * 40},
            {"labels": []},
            {"steps": []},
            {"status": "queued"},
        ):
            self.assertEqual(
                classify_job(dict(self.job, **change), self.sha, lane)[0], "blocked"
            )
        self.assertEqual(
            classify_job(dict(self.job, conclusion="failure"), self.sha, lane)[0],
            "failed",
        )

    def test_24_cells_and_nine_scenarios_blocked_by_default(self):
        report = empty_report(self.cfg, {})
        self.assertEqual(len(report["coverage"]), 24)
        self.assertEqual(len(report["scenarios"]), 9)
        self.assertEqual({x["status"] for x in report["coverage"]}, {"blocked"})

    def test_authoritative_receipts_logs_and_overall_blocker(self):
        run = {
            "head_sha": self.sha,
            "path": ".github/workflows/fleet-qualification.yml",
            "event": "workflow_dispatch",
        }
        receipts = {"newdarwindev/runspindle/strix-linux-x64": 1}
        with tempfile.TemporaryDirectory() as directory, patch(
            "runner_fleet.evidence.gh_json", side_effect=[run, [{"jobs": [self.job]}]]
        ), patch(
            "runner_fleet.evidence.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, b"workload log"),
        ):
            report = collect(
                self.cfg, {"runspindle": self.sha}, receipts, Path(directory)
            )
            self.assertEqual(report["coverage"][0]["status"], "passed")
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                Path(report["coverage"][0]["log_path"]).read_bytes(), b"workload log"
            )
            self.assertEqual(
                Path(report["coverage"][0]["log_path"]).stat().st_mode & 0o777, 0o600
            )

    def test_bad_workflow_event_and_unavailable_logs_block(self):
        receipts = {"newdarwindev/runspindle/strix-linux-x64": 1}
        run = {
            "head_sha": self.sha,
            "path": ".github/workflows/fleet-qualification.yml",
            "event": "workflow_dispatch",
        }
        with tempfile.TemporaryDirectory() as directory:
            for field, value in (
                ("head_sha", "wrong"),
                ("path", "other.yml"),
                ("event", "pull_request"),
            ):
                with patch(
                    "runner_fleet.evidence.gh_json",
                    return_value=dict(run, **{field: value}),
                ):
                    self.assertEqual(
                        collect(
                            self.cfg,
                            {"runspindle": self.sha},
                            receipts,
                            Path(directory),
                        )["coverage"][0]["status"],
                        "blocked",
                    )
            with patch(
                "runner_fleet.evidence.gh_json",
                side_effect=[run, [{"jobs": [self.job]}]],
            ), patch(
                "runner_fleet.evidence.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gh"),
            ):
                self.assertEqual(
                    collect(
                        self.cfg, {"runspindle": self.sha}, receipts, Path(directory)
                    )["coverage"][0]["status"],
                    "blocked",
                )


class CommandTests(unittest.TestCase):
    def test_install_and_qualify_do_not_claim_success(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(
            io.StringIO()
        ):
            with patch(
                "fleet.inspect_host", return_value={"status": "blocked", "checks": []}
            ):
                self.assertEqual(
                    fleet.main(
                        ["install", "--host", "macbook", "--state-dir", directory]
                    ),
                    2,
                )
            self.assertEqual(
                fleet.main(["qualify", "--host", "macbook", "--state-dir", directory]),
                2,
            )
            report = json.loads((Path(directory) / "qualification.json").read_text())
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                fleet.main(["drain", "--host", "macbook", "--state-dir", directory]), 0
            )
            state = json.loads((Path(directory) / "macbook-control.json").read_text())
            self.assertEqual(state["observed_state"], "not_installed")
            self.assertEqual(
                (Path(directory) / "qualification.json").stat().st_mode & 0o777, 0o600
            )

    def test_preflight_cannot_be_bypassed_with_interactive_credentials(self):
        cfg = config()
        with tempfile.TemporaryDirectory() as directory, patch(
            "runner_fleet.preflight.Path.read_text",
            return_value="MemAvailable: 25000000 kB\n",
        ), patch("runner_fleet.preflight.platform.system", return_value="Linux"), patch(
            "runner_fleet.preflight.platform.machine", return_value="x86_64"
        ), patch(
            "runner_fleet.preflight.shutil.which", return_value="/tool"
        ), patch(
            "runner_fleet.preflight.run",
            return_value=subprocess.CompletedProcess([], 0, "true\n"),
        ):
            report = inspect_host(cfg, "strix", Path(directory))
            self.assertEqual(report["status"], "blocked")
            checks = {c["check"]: c for c in report["checks"]}
            self.assertEqual(checks["admin:runspindle"]["status"], "passed")
            self.assertEqual(checks["network_isolation"]["status"], "blocked")
            self.assertEqual(checks["background_credentials"]["status"], "blocked")


class FailureBoundaryTests(unittest.TestCase):
    def test_mac_pressure_and_broken_probe_fail_closed(self):
        cfg = config()

        def mac_run(argv):
            if argv[0] == "vm_stat":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 100.\nPages inactive: 100.\n",
                )
            if argv[0] == "sysctl":
                return subprocess.CompletedProcess(argv, 0, "1\n")
            raise subprocess.TimeoutExpired(argv, 20)

        with tempfile.TemporaryDirectory() as directory, patch(
            "runner_fleet.preflight.platform.system", return_value="Darwin"
        ), patch(
            "runner_fleet.preflight.platform.machine", return_value="arm64"
        ), patch(
            "runner_fleet.preflight.shutil.which", return_value="/tool"
        ), patch(
            "runner_fleet.preflight.run", side_effect=mac_run
        ):
            report = inspect_host(cfg, "macbook", Path(directory))
            checks = {c["check"]: c for c in report["checks"]}
            self.assertEqual(checks["virtualization"]["status"], "passed")
            self.assertEqual(checks["memory"]["status"], "blocked")
            self.assertEqual(checks["admin:runspindle"]["status"], "blocked")
            with patch(
                "runner_fleet.preflight.run", side_effect=OSError("unavailable")
            ), patch(
                "runner_fleet.preflight.shutil.disk_usage",
                side_effect=OSError("unavailable"),
            ):
                checks = {
                    c["check"]: c
                    for c in inspect_host(cfg, "macbook", Path(directory))["checks"]
                }
                self.assertEqual(checks["virtualization"]["status"], "blocked")
                self.assertEqual(checks["disk"]["status"], "blocked")
                self.assertEqual(checks["memory"]["status"], "blocked")

    def test_strix_controls_use_observed_service_backend(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            args = ["--host", "strix", "--state-dir", directory]
            with patch("runner_fleet.service.control", return_value={"status": "blocked"}) as control:
                self.assertEqual(fleet.main(["resume", *args]), 2)
                control.assert_called_once()
            with patch("runner_fleet.service.observed", return_value={"status": "blocked"}):
                self.assertEqual(fleet.main(["status", *args]), 2)
            with patch("runner_fleet.service.uninstall", return_value={"status": "blocked"}):
                self.assertEqual(fleet.main(["uninstall", *args]), 2)

    def test_reject_unknown_or_unpinned_revisions(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(
            io.StringIO()
        ), contextlib.redirect_stderr(io.StringIO()):
            path = Path(directory) / "revisions.json"
            path.write_text(json.dumps({"runspindle": "main"}))
            with self.assertRaises(SystemExit):
                fleet.main(
                    [
                        "qualify",
                        "--host",
                        "strix",
                        "--state-dir",
                        directory,
                        "--revisions",
                        str(path),
                    ]
                )
            path.write_text(json.dumps({"untrusted": "a" * 40}))
            with self.assertRaises(SystemExit):
                fleet.main(
                    [
                        "qualify",
                        "--host",
                        "strix",
                        "--state-dir",
                        directory,
                        "--revisions",
                        str(path),
                    ]
                )

    def test_pending_job_and_ambiguous_jobs_do_not_qualify(self):
        cfg = config()
        sha = "a" * 40
        receipts = {"newdarwindev/runspindle/strix-linux-x64": 1}
        run = {
            "head_sha": sha,
            "path": ".github/workflows/fleet-qualification.yml",
            "event": "push",
        }
        job = {"name": "strix-linux-x64", "status": "queued", "head_sha": sha}
        with tempfile.TemporaryDirectory() as directory:
            for jobs in ([], [job, job], [job]):
                with patch(
                    "runner_fleet.evidence.gh_json", side_effect=[run, [{"jobs": jobs}]]
                ):
                    row = collect(cfg, {"runspindle": sha}, receipts, Path(directory))[
                        "coverage"
                    ][0]
                    self.assertEqual(row["status"], "blocked")
                    self.assertNotIn("log_path", row)
            row = collect(
                cfg,
                {"runspindle": sha},
                {"newdarwindev/runspindle/strix-linux-x64": "invalid"},
                Path(directory),
            )["coverage"][0]
            self.assertEqual(row["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
