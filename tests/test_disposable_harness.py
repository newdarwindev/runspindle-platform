"""Issue #1 synthetic acceptance; no dependency/service/native integration."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.disposable_harness.contracts import (
    DEPENDENCIES,
    TARGETS,
    canonical,
    digest,
    integration_plan,
    load_manifest,
    parse,
    toolchain_status,
)
from tools.disposable_harness.packaging import FakeAdapters, FakeInstaller, make_bundle
from tools.disposable_harness.runtime import Clock, DependencyDouble, DisposableRun
from tools.disposable_harness.telemetry import (
    CANARY,
    Backend,
    Pipeline,
    canary_paths,
    produce,
    sanitize,
)
from tools.runner_fleet.model import admissible_mac_job, config, matching_lane

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures/harness"


class ManifestTests(unittest.TestCase):
    def test_complete_inventory_and_optional_upstream_are_separate(self) -> None:
        manifest, files = load_manifest(FIXTURES)
        self.assertEqual(set(manifest["dependencies"]), DEPENDENCIES)
        self.assertEqual(set(files), {"NOTICE.txt", "dependency.txt"})
        for name in DEPENDENCIES:
            self.assertEqual(integration_plan(name)["status"], "not_run")
            self.assertEqual(integration_plan(name, opt_in=True)["status"], "blocked")
            self.assertEqual(
                integration_plan(name, opt_in=True)["compatibility"], "unknown"
            )

    def test_corrupted_floating_missing_license_and_toolchain_fail_closed(self) -> None:
        manifest, files = load_manifest(FIXTURES)
        mutations: list[Callable[[dict[str, Any]], Any]] = [
            lambda m: m["files"].update({"dependency.txt": "0" * 64}),
            lambda m: m["dependencies"]["Temporal"].update(version="latest"),
            lambda m: m["dependencies"]["OpenBao"].update(license="UNKNOWN"),
            lambda m: m["dependencies"]["DSPy"].update(source_revision="main"),
            lambda m: m["dependencies"].pop("Langfuse"),
            lambda m: m["toolchain"].update(version="latest"),
            lambda m: m["targets"].append("windows/x64"),
            lambda m: m["sbom"].update(notice_sha256="f" * 64),
            lambda m: m.update(evidence_class="upstream_integration"),
        ]
        for mutate in mutations:
            with (
                self.subTest(mutate=mutate),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                for name, data in files.items():
                    (root / name).write_bytes(data)
                candidate = copy.deepcopy(manifest)
                mutate(candidate)
                (root / "manifest.json").write_bytes(canonical(candidate))
                with self.assertRaises(ValueError):
                    load_manifest(root)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse(b'{"schema":1,"schema":1}')

    def test_unqualified_interpreter_is_blocked(self) -> None:
        with patch(
            "tools.disposable_harness.contracts.platform.python_version",
            return_value="0.0.0",
        ):
            self.assertEqual(toolchain_status()["status"], "blocked")

    def test_symlinks_and_inventory_escape_rejected(self) -> None:
        manifest, files = load_manifest(FIXTURES)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data in files.items():
                (root / name).write_bytes(data)
            (root / "manifest.json").write_bytes(canonical(manifest))
            (root / "dependency.txt").unlink()
            (root / "dependency.txt").symlink_to(FIXTURES / "dependency.txt")
            with self.assertRaisesRegex(ValueError, "symlink"):
                load_manifest(root)
            manifest["files"]["../outside"] = "a" * 64
            (root / "manifest.json").write_bytes(canonical(manifest))
            with self.assertRaises(ValueError):
                load_manifest(root)


class ReceiptTests(unittest.TestCase):
    def test_failed_or_cancelled_run_cannot_retain_old_success(self) -> None:
        from tools import check_disposable_harness as command

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            for error in (ValueError("invalid fixture"), KeyboardInterrupt()):
                path.write_text('{"status":"passed"}')
                with (
                    patch.object(sys, "argv", ["check", "--output", str(path)]),
                    patch.object(command, "source", side_effect=error),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertNotEqual(command.main(), 0)
                self.assertEqual(json.loads(path.read_text())["status"], "failed")

    def test_unsupported_toolchain_records_blocked_without_running_tests(self) -> None:
        from tools import check_disposable_harness as command

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            with (
                patch.object(command, "source", return_value={}),
                patch.object(
                    command, "toolchain_status", return_value={"status": "blocked"}
                ),
                patch.object(unittest.defaultTestLoader, "discover") as discover,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(command.execute(path), 2)
                discover.assert_not_called()
            self.assertEqual(json.loads(path.read_text())["status"], "blocked")


class LifecycleTests(unittest.TestCase):
    def test_all_dependency_faults_are_bounded_and_cleaned(self) -> None:
        for upstream in sorted(DEPENDENCIES):
            for mode in sorted(DependencyDouble.MODES):
                with self.subTest(upstream=upstream, mode=mode):
                    run = DisposableRun(upstream, Clock())
                    try:
                        with run:
                            endpoint = run.register(
                                "dependency", DependencyDouble(run.clock, mode)
                            )
                            run.ready(lambda: True)
                            if mode in {"unavailable", "slow", "tls_denied"}:
                                with self.assertRaises(
                                    (ConnectionError, TimeoutError, PermissionError)
                                ):
                                    run.request(endpoint, b"synthetic")
                            else:
                                code, body = run.request(endpoint, b"synthetic")
                                if mode == "malformed":
                                    with self.assertRaises(ValueError):
                                        json.loads(body)
                                else:
                                    self.assertEqual(
                                        code, 401 if mode == "auth_denied" else 200
                                    )
                    finally:
                        self.assertTrue(run.closed)
                        self.assertFalse(run.processes | run.services | run.volumes)
                        self.assertFalse(run.endpoints)
                        self.assertIsNotNone(run.root)
                        self.assertFalse(run.root.exists())

    def test_egress_and_cross_run_denied_and_no_socket_created(self) -> None:
        with patch("socket.socket", side_effect=AssertionError("unexpected socket")):
            with (
                DisposableRun("one", Clock()) as first,
                DisposableRun("two", Clock()) as second,
            ):
                endpoint = first.register(
                    "backend", DependencyDouble(first.clock, "healthy")
                )
                for target in (
                    endpoint,
                    "https://example.invalid",
                    "http://127.0.0.1:4318",
                    "fixture://two/missing",
                ):
                    with self.assertRaises(PermissionError):
                        second.request(target, b"synthetic")
                self.assertEqual(second.outbound_denied, 4)
                self.assertEqual(first.request(endpoint, b"synthetic")[0], 200)

    def test_readiness_budget_resource_limits_and_failure_cancel_cleanup(self) -> None:
        for error in (RuntimeError, KeyboardInterrupt):
            run = DisposableRun("cancel", Clock())
            with self.assertRaises(error):
                with run:
                    run.register("backend", DependencyDouble(run.clock, "healthy"))
                    (run.root / "scratch").write_bytes(b"synthetic")
                    raise error("synthetic interruption")
            self.assertFalse(run.root.exists())
            self.assertFalse(run.processes | run.services | run.volumes)
        with DisposableRun("bounds", Clock(), deadline=1) as run:
            with self.assertRaises(TimeoutError):
                run.ready(lambda: False)
        with DisposableRun("bounds", Clock()) as run:
            for index in range(8):
                endpoint = run.register(
                    str(index), DependencyDouble(run.clock, "healthy")
                )
            with self.assertRaises(ValueError):
                run.register("extra", DependencyDouble(run.clock, "healthy"))
            with self.assertRaises(ValueError):
                run.request(endpoint, b"x" * 65537)

    @unittest.skipUnless(os.name == "posix", "POSIX signal cleanup probe required")
    def test_sigterm_unwinds_real_temporary_volume(self) -> None:
        code = """
import signal, time, sys
from pathlib import Path
from tools.disposable_harness.runtime import Clock, DisposableRun, DependencyDouble
def cancelled(*_):
    raise KeyboardInterrupt()
signal.signal(signal.SIGTERM, cancelled)
try:
    with DisposableRun('child', Clock(), parent=Path(sys.argv[1])) as run:
        run.register('backend', DependencyDouble(run.clock, 'healthy'))
        (run.root / 'scratch').write_bytes(b'synthetic')
        print(run.root, flush=True)
        time.sleep(20)
except KeyboardInterrupt:
    pass
"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "child-path"
            with output.open("w") as stream:
                child = subprocess.Popen(
                    [sys.executable, "-c", code, directory],
                    cwd=ROOT,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 5
                    while (
                        not output.read_text().strip()
                        and time.monotonic() < deadline
                        and child.poll() is None
                    ):
                        time.sleep(0.01)
                    self.assertTrue(
                        output.read_text().strip(), "child failed before readiness"
                    )
                    path = Path(output.read_text().strip())
                    self.assertTrue((path / "scratch").is_file())
                    child.send_signal(signal.SIGTERM)
                    _, stderr = child.communicate(timeout=5)
                    self.assertEqual(child.returncode, 0, stderr)
                    self.assertFalse(path.exists())
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=5)

    @unittest.skipUnless(os.name == "posix", "POSIX supervisor cleanup probe required")
    def test_supervisor_cleans_volume_after_worker_is_killed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="synthetic-supervisor-") as directory:
            supervisor_root = Path(directory)
            code = """
import sys, time
from pathlib import Path
from tools.disposable_harness.runtime import Clock, DisposableRun
with DisposableRun('killed', Clock(), parent=Path(sys.argv[1])) as run:
    (Path(sys.argv[1]) / 'ready').write_text(str(run.root))
    time.sleep(20)
"""
            child = subprocess.Popen(
                [sys.executable, "-c", code, directory],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                end = time.monotonic() + 5
                while (
                    not (supervisor_root / "ready").exists()
                    and child.poll() is None
                    and time.monotonic() < end
                ):
                    time.sleep(0.01)
                self.assertTrue((supervisor_root / "ready").exists())
                child.kill()
                child.communicate(timeout=5)
                self.assertEqual(child.returncode, -signal.SIGKILL)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
        self.assertFalse(supervisor_root.exists())


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.runtime = self.stack.enter_context(DisposableRun("telemetry", Clock()))
        self.backend = Backend(self.runtime.root / "backend.sqlite")
        self.stack.callback(self.backend.close)
        self.endpoint = self.runtime.register("backend", self.backend.receive)
        self.spool = self.runtime.root / "spool.sqlite"
        self.pipeline = Pipeline(self.runtime, self.endpoint, self.spool)
        self.stack.callback(self.pipeline.close)

    def receipt(self) -> dict[str, Any]:
        return self.pipeline.receipt(self.backend.path)

    def test_queue_flush_shutdown_and_fresh_readback(self) -> None:
        self.pipeline.emit(produce(17), units=7)
        self.assertEqual(self.receipt()["queued"], 1)
        self.assertEqual(self.receipt()["persisted"], 0)
        self.pipeline.shutdown()
        self.pipeline = Pipeline(self.runtime, self.endpoint, self.spool)
        self.stack.callback(self.pipeline.close)
        observed = self.receipt()
        self.assertEqual(
            (
                observed["emitted"],
                observed["accepted"],
                observed["persisted"],
                observed["usage_units"],
            ),
            (1, 1, 1, 7),
        )

    def test_http_acceptance_without_readback_is_loss_not_usage(self) -> None:
        self.backend.mode = "accept_only"
        self.pipeline.emit(produce(17), units=8)
        self.pipeline.flush()
        observed = self.receipt()
        self.assertEqual(observed["accepted"], 1)
        self.assertEqual(observed["persisted"], 0)
        self.assertEqual(observed["usage_units"], 0)
        self.assertEqual(observed["events"][0]["loss"], "accepted_without_readback")

    def test_unavailable_reject_malformed_and_recovery(self) -> None:
        self.backend.mode = "unavailable"
        self.pipeline.emit(produce(17))
        self.pipeline.flush()
        self.assertEqual(self.receipt()["queued"], 1)
        self.assertEqual(self.receipt()["persisted"], 0)
        self.backend.mode = "reject"
        self.pipeline.flush()
        self.assertEqual(self.receipt()["rejected"], 1)
        self.backend.mode = "persist"
        self.assertEqual(self.runtime.request(self.endpoint, b"{malformed")[0], 400)
        self.assertEqual(
            self.runtime.request(self.endpoint, canonical(produce(17)))[0], 400
        )
        self.pipeline.emit(produce(17, 2))
        self.pipeline.flush()
        self.assertEqual(self.receipt()["persisted"], 1)
        self.assertEqual(
            self.backend.db.execute("SELECT count(*) FROM records").fetchone()[0], 1
        )

    def test_restart_replay_duplicates_do_not_double_count_usage(self) -> None:
        self.pipeline.emit(produce(17), units=7)
        self.pipeline.emit(produce(17), units=7)
        with self.assertRaises(InterruptedError):
            self.pipeline.flush(interrupt_after_accept=True)
        self.assertEqual(
            (self.receipt()["queued"], self.receipt()["persisted"]), (1, 1)
        )
        self.pipeline.close()
        self.pipeline = Pipeline(self.runtime, self.endpoint, self.spool)
        self.stack.callback(self.pipeline.close)
        self.pipeline.flush()
        observed = self.receipt()
        self.assertEqual(
            (observed["emitted"], observed["persisted"], observed["usage_units"]),
            (1, 1, 7),
        )
        self.assertEqual(observed["events"][0]["copies"], 2)
        self.assertEqual(observed["events"][0]["duplicates"], 1)
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            self.pipeline.emit(produce(17), units=8)

    def test_queue_bound_and_independent_persisted_bytes_loss(self) -> None:
        for number in range(1, 33):
            self.pipeline.emit(produce(17, number))
        with self.assertRaisesRegex(ValueError, "queue resource bound"):
            self.pipeline.emit(produce(17, 33))
        self.pipeline.flush()
        self.assertEqual(self.receipt()["persisted"], 32)
        self.backend.db.execute("DELETE FROM records")
        self.backend.db.commit()
        self.assertEqual(self.receipt()["persisted"], 0)
        self.assertEqual(self.receipt()["usage_units"], 0)

    def test_every_correlation_field_is_required_and_unmatched_canaries_block(
        self,
    ) -> None:
        clean = sanitize(produce(17))
        self.pipeline.emit(produce(17))
        context = clean["resourceSpans"][0]["resource"]["attributes"]
        span = clean["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        candidates = []
        for index in range(len(context)):
            changed = copy.deepcopy(clean)
            changed["resourceSpans"][0]["resource"]["attributes"][index]["value"][
                "stringValue"
            ] += "wrong"
            candidates.append(changed)
        for field in ("traceId", "spanId"):
            changed = copy.deepcopy(clean)
            changed["resourceSpans"][0]["scopeSpans"][0]["spans"][0][field] = "0" * len(
                span[field]
            )
            candidates.append(changed)
        changed = copy.deepcopy(clean)
        changed["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"][0][
            "value"
        ]["stringValue"] = "wrong"
        candidates.append(changed)
        for changed in candidates:
            self.backend.db.execute(
                "INSERT INTO records VALUES (?)", (canonical(changed),)
            )
        self.backend.db.execute(
            "INSERT INTO records VALUES (?)", (canonical({"unmatched": CANARY}),)
        )
        self.backend.db.commit()
        self.assertEqual(self.receipt()["persisted"], 0)
        self.assertEqual(self.receipt()["privacy"], "blocked")

    def test_whole_envelope_canaries_and_ambiguous_identity(self) -> None:
        raw = produce(17)
        paths = canary_paths(raw)
        for expected in ("schemaUrl", "metadata", "links", "events", "status", "path"):
            self.assertTrue(any(expected in path for path in paths), expected)
        span = raw["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual(
            span["attributes"][1],
            {"key": "tool_error", "value": {"stringValue": CANARY}},
        )
        self.assertFalse(canary_paths(sanitize(raw)))
        self.pipeline.emit(raw)
        self.pipeline.flush()
        self.assertEqual(self.receipt()["privacy"], "passed_synthetic_only")
        attrs = raw["resourceSpans"][0]["resource"]["attributes"]
        attrs.append(copy.deepcopy(attrs[0]))
        with self.assertRaisesRegex(ValueError, "ambiguous identity"):
            self.pipeline.emit(raw)


class PackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        _, self.files = load_manifest(FIXTURES)

    def installer(self, target: str) -> FakeInstaller:
        return FakeInstaller(
            target,
            {"fixture-runtime": "synthetic-v1"},
            FakeAdapters(files={"unrelated": b"preserved"}),
        )

    def test_macos_linux_upgrade_rollback_and_service_double(self) -> None:
        for target in sorted(TARGETS):
            with self.subTest(target=target):
                installer = self.installer(target)
                installer.install(make_bundle(target, self.files))
                first = copy.deepcopy(installer.adapters)
                second_files = {**self.files, "dependency.txt": b"synthetic-upgrade"}
                installer.install(
                    make_bundle(target, second_files, version="synthetic-2")
                )
                self.assertEqual(
                    installer.adapters.files["dependency.txt"], b"synthetic-upgrade"
                )
                self.assertEqual(
                    installer.adapters.services["synthetic"],
                    "launchd" if target.startswith("darwin/") else "systemd",
                )
                installer.rollback()
                self.assertEqual(installer.adapters, first)
                with self.assertRaises(ValueError):
                    installer.rollback()

    def test_failures_cancellation_at_every_install_boundary_restore_prior_state(
        self,
    ) -> None:
        for target in sorted(TARGETS):
            for boundary in (
                "intent",
                "file:NOTICE.txt",
                "file:dependency.txt",
                "migration",
                "service",
                "activated",
            ):
                for exception in (RuntimeError, KeyboardInterrupt):
                    with self.subTest(
                        target=target, boundary=boundary, exception=exception
                    ):
                        installer = self.installer(target)
                        installer.install(make_bundle(target, self.files))
                        prior = copy.deepcopy(installer.adapters)

                        def fail(stage: str) -> None:
                            if stage == boundary:
                                raise exception("synthetic fault")

                        with self.assertRaises(exception):
                            installer.install(
                                make_bundle(
                                    target,
                                    {**self.files, "dependency.txt": b"next"},
                                    version="synthetic-2",
                                ),
                                fail,
                            )
                        self.assertEqual(installer.adapters, prior)

    def test_admission_fails_before_any_effect_even_if_rehashed(self) -> None:
        for target in sorted(TARGETS):
            mutations: list[Callable[[dict[str, Any]], Any]] = [
                lambda b: b["files"].update({"dependency.txt": b"corrupt"}),
                lambda b: b["manifest"].update(target="windows/x64"),
                lambda b: b["manifest"].update(
                    target=target.replace("amd64", "arm64")
                    if target.endswith("amd64")
                    else target.replace("arm64", "amd64")
                ),
                lambda b: b["manifest"]["dependencies"].update(
                    {"fixture-runtime": "latest"}
                ),
                lambda b: b["manifest"]["sbom"].update(license="UNKNOWN"),
                lambda b: b["manifest"]["sbom"].update(components=[]),
                lambda b: b["manifest"].update(version="latest"),
            ]
            for mutate in mutations:
                installer = self.installer(target)
                prior = copy.deepcopy(installer.adapters)
                value = make_bundle(target, self.files)
                mutate(value)
                value["checksum"] = digest(canonical(value["manifest"]))
                with self.assertRaises(ValueError):
                    installer.install(value)
                self.assertEqual(installer.adapters, prior)
            installer = self.installer(target)
            installer.dependencies.clear()
            with self.assertRaisesRegex(ValueError, "missing dependencies"):
                installer.install(make_bundle(target, self.files))
            self.assertIsNone(installer.adapters.active)
            installer = self.installer(target)
            invalid = make_bundle(target, self.files)
            invalid["checksum"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "manifest checksum"):
                installer.install(invalid)
            invalid = make_bundle(target, {**self.files, "a": b"x", "a/b": b"y"})
            with self.assertRaisesRegex(ValueError, "ancestor collision"):
                installer.install(invalid)
            self.assertEqual(installer.adapters.files, {"unrelated": b"preserved"})
        for path in ("../outside", "/outside", ".", "a/../b", "a//b"):
            with self.subTest(path=path):
                value = make_bundle("linux/amd64", {**self.files, path: b"x"})
                with self.assertRaises(ValueError):
                    self.installer("linux/amd64").install(value)


class RunnerPolicyTests(unittest.TestCase):
    def test_current_config_exact_labels_and_fork_isolation(self) -> None:
        cfg = config()
        for lane, policy in cfg["lanes"].items():
            self.assertEqual(matching_lane(cfg, policy["host"], policy["labels"]), lane)
            for labels in (
                ["ubuntu-latest"],
                ["self-hosted"],
                policy["labels"] + ["personal"],
                policy["labels"][:-1],
            ):
                self.assertIsNone(matching_lane(cfg, policy["host"], labels))
        lane = "macbook-macos-arm64"
        job = {
            "repository": "runspindle-platform",
            "head_repository": "newdarwindev/runspindle-platform",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "labels": cfg["lanes"][lane]["labels"],
            "name": lane,
            "workflow_path": ".github/workflows/fleet-qualification.yml",
        }
        self.assertTrue(admissible_mac_job(cfg, job))
        for change in (
            {"head_repository": "outsider/runspindle-platform"},
            {"event": "pull_request"},
            {"event": "pull_request_target"},
            {"head_branch": "feature/unreviewed"},
        ):
            self.assertFalse(admissible_mac_job(cfg, {**job, **change}))

    def test_actual_workflows_remain_local_and_no_public_fork_trigger(self) -> None:
        # Structural fixture assertion, not authoritative runner provisioning.
        import re

        allowed = {tuple(lane["labels"]) for lane in config()["lanes"].values()}
        workflows = list((ROOT / ".github/workflows").glob("*.yml"))
        self.assertTrue(workflows)
        for path in workflows:
            source = path.read_text()
            lanes = re.findall(r"^\s*runs-on:\s*\[([^\]]+)\]$", source, re.M)
            self.assertTrue(lanes, path)
            self.assertEqual(len(lanes), len(re.findall(r"^\s*runs-on:", source, re.M)))
            for labels in lanes:
                self.assertIn(tuple(s.strip() for s in labels.split(",")), allowed)
            self.assertNotRegex(source, r"pull_request(?:_target)?\s*:")
            self.assertIn("persist-credentials: false", source)
            self.assertIn("contents: read", source)


if __name__ == "__main__":
    unittest.main()
