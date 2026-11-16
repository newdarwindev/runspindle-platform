"""Synthetic boundary checks; real lane tests opt in with an explicit prepared dir."""

import importlib.util
import json
import os
import pathlib
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from typing import Any, Callable
from unittest.mock import patch

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools/temporal_lane.py"
spec = importlib.util.spec_from_file_location("temporal_lane", MODULE_PATH)
lane_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane_module)


def signal_child_script(prepared: str) -> str:
    return (
        "import importlib.util,json,time\n"
        + f"s=importlib.util.spec_from_file_location('lane',{str(MODULE_PATH)!r})\n"
        + "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
        + f"with m.Lane({prepared!r}) as lane:\n"
        + ' print(json.dumps({"names":[lane.server,lane.volume,lane.network],"diagnostics":str(lane.diagnostics) if lane.retain_diagnostics else None}),flush=True)\n'
        + " time.sleep(120)\n"
    )


class BoundaryTests(unittest.TestCase):
    def test_offline_preparation_never_downloads_missing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(lane_module, "command") as command:
                with self.assertRaisesRegex(
                    lane_module.Blocked, "offline Temporal archive missing"
                ):
                    lane_module.prepare(directory, "amd64", offline=True)
                command.assert_not_called()

    def test_floating_image_denied_before_docker(self):
        with patch.object(lane_module, "docker") as docker:
            with self.assertRaises(lane_module.Blocked):
                lane_module.Lane._image("temporalio/temporal:latest")
            docker.assert_not_called()

    def test_corrupted_download_never_builds_or_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = lane_module.read_pins()["cli"]["artifacts"]["arm64"]["filename"]
            (pathlib.Path(directory) / artifact).write_bytes(b"synthetic-corrupt")
            with patch.object(lane_module, "docker") as docker:
                with self.assertRaisesRegex(lane_module.Blocked, "checksum"):
                    lane_module.prepare(directory, "arm64")
                docker.assert_not_called()

    def test_changed_prepared_manifest_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / "prepared.json").write_text(
                json.dumps({"pins_sha256": "0" * 64})
            )
            with self.assertRaisesRegex(lane_module.Blocked, "manifest changed"):
                lane_module.Lane(directory)

    def test_changed_prepared_binary_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "prepared.json").write_text(
                json.dumps(
                    {
                        "pins_sha256": lane_module.digest(lane_module.PINS),
                        "binary_sha256": "0" * 64,
                    }
                )
            )
            (root / "temporal").write_bytes(b"synthetic-corrupt")
            with self.assertRaisesRegex(lane_module.Blocked, "executable changed"):
                lane_module.Lane(directory)

    def test_partial_creation_lost_reply_is_cleaned(self):
        # Inject a lost response after resource allocation, at each boundary.
        for fault in ("network", "volume", "run"):
            with self.subTest(fault=fault):
                lane = object.__new__(lane_module.Lane)
                lane._created = False
                lane._handlers = {}
                lane.network, lane.volume, lane.server = (
                    "synthetic-net",
                    "synthetic-db",
                    "synthetic-server",
                )
                lane.clients = set()
                lane.image = "sha256:" + "0" * 64
                calls = []

                def fake(*args, **kwargs):
                    calls.append(args)
                    if args[0] == fault and ("create" in args or fault == "run"):
                        raise subprocess.TimeoutExpired(
                            "synthetic-docker-lost-reply", 1
                        )
                    return subprocess.CompletedProcess(args, 0, "", "")

                with patch.object(lane_module, "docker", fake):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        lane.start()
                self.assertIn(("rm", "--force", lane.server), calls)
                self.assertIn(("volume", "rm", "--force", lane.volume), calls)
                self.assertIn(("network", "rm", lane.network), calls)

    def test_failed_client_delete_retains_cleanup_ownership(self):
        lane = object.__new__(lane_module.Lane)
        name = "synthetic-client"
        lane.clients = {name}

        def fake(*args, **kwargs):
            if args[0] == "rm":
                return subprocess.CompletedProcess(
                    args, 1, "", "synthetic transient deletion denial"
                )
            return subprocess.CompletedProcess(args, 0, "0", "")

        with (
            patch.object(lane, "spawn_client", return_value=name),
            patch.object(lane_module, "docker", fake),
        ):
            with self.assertRaisesRegex(RuntimeError, "client cleanup incomplete"):
                lane.run_client("sha256:" + "0" * 64, ["synthetic"])
        self.assertIn(name, lane.clients)

    def test_cleanup_failure_does_not_report_success(self):
        lane = object.__new__(lane_module.Lane)
        lane._created, lane._handlers, lane.clients = True, {}, set()
        lane.server, lane.volume, lane.network = "server", "volume", "network"
        with patch.object(
            lane_module,
            "docker",
            return_value=subprocess.CompletedProcess(
                [], 1, "", "synthetic daemon unavailable"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "cleanup incomplete"):
                lane.close()


class RetentionBoundaryTests(unittest.TestCase):
    def fake_docker(
        self, calls: list[tuple[object, ...]], changing_logs: bool = False
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        reads = 0
        running: dict[str, bool] = {}

        def fake(*args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal reads
            calls.append(args)
            stdout = ""
            if args[:2] == ("container", "inspect"):
                name = str(args[2])
                stdout = json.dumps(
                    [
                        {
                            "Name": "/" + name,
                            "Id": "container-id-" + name,
                            "State": {
                                "Running": running.get(name, False),
                                "Paused": False,
                                "Restarting": False,
                                "Pid": 123 if running.get(name, False) else 0,
                                "Status": "running"
                                if running.get(name, False)
                                else "exited",
                                "ExitCode": 0,
                            },
                            "HostConfig": {
                                "RestartPolicy": {"Name": "no"},
                                "LogConfig": {
                                    "Type": "json-file",
                                    "Config": {"max-size": "-1"},
                                },
                            },
                        }
                    ]
                )
            elif args[0] == "logs":
                reads += 1
                stdout = (
                    "synthetic log changed"
                    if changing_logs and reads > 1
                    else "synthetic output\n"
                )
            elif args[0] == "stop":
                running[str(args[-1])] = False
            elif args[0] == "start":
                running[str(args[-1])] = False
            elif args[0] == "wait":
                stdout = "0\n"
            elif args[:2] == ("volume", "inspect"):
                stdout = json.dumps(
                    [
                        {
                            "Name": str(args[2]),
                            "Driver": "local",
                            "CreatedAt": "synthetic-created-at",
                            "Mountpoint": "/synthetic/volume",
                            "Scope": "local",
                            "Labels": {"runspindle.synthetic": "true"},
                            "Options": None,
                        }
                    ]
                )
            elif args[:2] == ("network", "inspect"):
                stdout = json.dumps(
                    [
                        {
                            "Name": str(args[2]),
                            "Id": "a" * 64,
                            "Created": "synthetic-created-at",
                            "Driver": "bridge",
                            "Scope": "local",
                            "Internal": True,
                            "EnableIPv6": False,
                            "Labels": {"runspindle.synthetic": "true"},
                            "Options": {
                                "com.docker.network.bridge.gateway_mode_ipv4": "isolated"
                            },
                        }
                    ]
                )
            elif args[:2] == ("network", "create"):
                stdout = "a" * 64

            return subprocess.CompletedProcess(list(map(str, args)), 0, stdout, "")

        return fake

    def retained_lane(self, directory: pathlib.Path) -> Any:
        lane = object.__new__(lane_module.Lane)
        lane.directory = directory
        lane.name = "synthetic-lane"
        lane.server, lane.volume, lane.network = (
            "synthetic-server",
            "synthetic-volume",
            "synthetic-network",
        )
        lane.clients = set()
        lane.owned_containers = {lane.server}
        lane.archived_containers = {}
        lane.retain_diagnostics = True
        lane.retention_error = None
        observations = self.fake_docker([])
        lane.auxiliary_identities = {
            kind: lane_module.auxiliary_identity(
                kind, json.loads(observations(kind, "inspect", name).stdout)[0]
            )
            for kind, name in (("volume", lane.volume), ("network", lane.network))
        }
        lane.diagnostics = lane_module.diagnostic_directory(directory, lane.name)
        lane._created, lane._handlers = True, {}
        return lane

    def test_optional_real_child_program_compiles_without_running_it(self) -> None:
        compile(
            signal_child_script("/synthetic/prepared"), "synthetic-signal-child", "exec"
        )

    def test_retention_is_explicit_and_rejects_unknown_policy(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(lane_module.retention_requested())
        with patch.dict(os.environ, {"RUNSPINDLE_TEMPORAL_RETAIN": "1"}):
            self.assertTrue(lane_module.retention_requested())
            self.assertFalse(lane_module.retention_requested(False))
        with patch.dict(os.environ, {"RUNSPINDLE_TEMPORAL_RETAIN": "unexpected"}):
            with self.assertRaises(ValueError):
                lane_module.retention_requested()

    def test_retained_close_keeps_originals_and_independently_checks_logs(self) -> None:
        calls: list[tuple[object, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            with patch.object(lane_module, "docker", self.fake_docker(calls)):
                lane.close()
                result = lane.verify_retained()
            self.assertEqual(
                result["classification"], "stopped_execution_retained_diagnostics"
            )
            self.assertEqual(
                result["artifacts"], {"volume": "retained", "network": "retained"}
            )
            self.assertFalse(any("rm" in call for call in calls))
            self.assertTrue(list(lane.diagnostics.glob("container-*/stdout.log")))

    def test_retained_restart_keeps_original_container_and_database(self) -> None:
        calls: list[tuple[object, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            original_server, original_volume = lane.server, lane.volume
            with (
                patch.object(lane_module, "docker", self.fake_docker(calls)),
                patch.object(lane, "_start_server"),
            ):
                lane.restart(abrupt=True)
            self.assertNotEqual(lane.server, original_server)
            self.assertIn(original_server, lane.archived_containers)
            self.assertEqual(lane.volume, original_volume)
            self.assertFalse(any("rm" in call for call in calls))

    def test_changed_logs_fail_without_removal_or_automatic_retry(self) -> None:
        calls: list[tuple[object, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            with patch.object(
                lane_module, "docker", self.fake_docker(calls, changing_logs=True)
            ):
                with self.assertRaisesRegex(RuntimeError, "retention incomplete"):
                    lane.close()
                count = len(calls)
                with self.assertRaisesRegex(RuntimeError, "no automatic retry"):
                    lane.close()
                self.assertEqual(len(calls), count)
            self.assertFalse(any("rm" in call for call in calls))

    def test_manually_tracked_client_is_stopped_retained_and_verified(self) -> None:
        calls: list[tuple[object, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            lane.clients.add("synthetic-raw-client")
            with patch.object(lane_module, "docker", self.fake_docker(calls)):
                lane.close()
                lane.verify_retained()
            self.assertIn("synthetic-raw-client", lane.archived_containers)
            self.assertFalse(any("rm" in call for call in calls))

    def test_stop_denial_is_not_replaced_by_delete_or_success(self) -> None:
        calls: list[tuple[object, ...]] = []
        ordinary = self.fake_docker(calls)

        def denied(*args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == "stop":
                calls.append(args)
                return subprocess.CompletedProcess(
                    [], 1, "", "synthetic stop access denied"
                )
            result = ordinary(*args, **kwargs)
            if args[:2] == ("container", "inspect"):
                value = json.loads(result.stdout)
                value[0]["State"].update(Running=True, Status="running", Pid=123)
                result.stdout = json.dumps(value)
            return result

        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            with patch.object(lane_module, "docker", denied):
                with self.assertRaisesRegex(RuntimeError, "retention incomplete"):
                    lane.close()
            self.assertFalse(any("rm" in call for call in calls))
            self.assertFalse(list(lane.diagnostics.glob("cleanup-*.json")))

    def test_client_timeout_still_retains_original_and_timeout_output(self) -> None:
        calls: list[tuple[object, ...]] = []
        ordinary = self.fake_docker(calls)
        name = "synthetic-client"

        def timeout(*args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == "wait":
                calls.append(args)
                raise subprocess.TimeoutExpired(
                    "synthetic wait", 1, output=b"synthetic partial output"
                )
            return ordinary(*args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            lane.clients.add(name)
            lane.owned_containers.add(name)
            with (
                patch.object(lane, "spawn_client", return_value=name),
                patch.object(lane_module, "docker", timeout),
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    lane.run_client("sha256:" + "a" * 64, ["synthetic"])
            self.assertIn(name, lane.archived_containers)
            self.assertNotIn(name, lane.clients)
            commands = [
                json.loads(path.read_text())
                for path in lane.diagnostics.glob("command-*.json")
            ]
            self.assertTrue(
                any(
                    "base64_original_bytes" in row.get("stdout", {}) for row in commands
                )
            )
            self.assertFalse(any("rm" in call for call in calls))

    def test_failed_restart_retention_cannot_be_retried_by_context_cleanup(
        self,
    ) -> None:
        calls: list[tuple[object, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            with patch.object(
                lane_module, "docker", self.fake_docker(calls, changing_logs=True)
            ):
                with self.assertRaisesRegex(RuntimeError, "no automatic retry"):
                    lane.restart(abrupt=True)
                count = len(calls)
                with self.assertRaisesRegex(RuntimeError, "no automatic retry"):
                    lane.close()
                self.assertEqual(len(calls), count)
            self.assertFalse(any("rm" in call for call in calls))

    def test_replaced_container_rejected_before_any_log_receipt(self) -> None:
        calls: list[tuple[object, ...]] = []
        ordinary = self.fake_docker(calls)
        inspections = 0

        def replaced(*args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal inspections
            result = ordinary(*args, **kwargs)
            if args[:2] == ("container", "inspect"):
                inspections += 1
                value = json.loads(result.stdout)
                if inspections > 1:
                    value[0]["Id"] = "synthetic-replacement"
                result.stdout = json.dumps(value)
            return result

        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            with patch.object(lane_module, "docker", replaced):
                with self.assertRaisesRegex(
                    RuntimeError, "replaced before log capture"
                ):
                    lane_module.archive_container(lane.server, lane.diagnostics)
            self.assertFalse(any(call[0] == "logs" for call in calls))
            self.assertFalse(
                list(lane.diagnostics.glob("container-*/retained-container.json"))
            )
            self.assertFalse(any("rm" in call for call in calls))

    def test_auxiliary_replacement_cannot_keep_original_resource_claim(self) -> None:
        for kind, field in (("volume", "CreatedAt"), ("network", "Id")):
            calls: list[tuple[object, ...]] = []
            ordinary = self.fake_docker(calls)

            def replaced(
                *args: object, **kwargs: Any
            ) -> subprocess.CompletedProcess[str]:
                result = ordinary(*args, **kwargs)
                if args[:2] == (kind, "inspect"):
                    value = json.loads(result.stdout)
                    value[0][field] = "synthetic-replacement"
                    result.stdout = json.dumps(value)
                return result

            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                lane = self.retained_lane(pathlib.Path(directory))
                with patch.object(lane_module, "docker", ordinary):
                    lane.close()
                with patch.object(lane_module, "docker", replaced):
                    with self.assertRaisesRegex(
                        RuntimeError, "auxiliary identity changed"
                    ):
                        lane.verify_retained()

    def test_creation_pins_both_auxiliary_observations(self) -> None:
        calls: list[tuple[object, ...]] = []
        with tempfile.TemporaryDirectory() as directory:
            lane = self.retained_lane(pathlib.Path(directory))
            lane._created = False
            lane.auxiliary_identities = {}
            with (
                patch.object(lane_module, "docker", self.fake_docker(calls)),
                patch.object(lane, "_start_server"),
            ):
                lane.start()
            self.assertEqual(set(lane.auxiliary_identities), {"volume", "network"})
            self.assertTrue((lane.diagnostics / "volume-identity.json").is_file())
            self.assertTrue((lane.diagnostics / "network-identity.json").is_file())

    def test_version_probe_retains_exited_original_without_auto_remove(self) -> None:
        calls: list[tuple[object, ...]] = []
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(lane_module, "docker", self.fake_docker(calls)),
        ):
            result = lane_module.retained_probe(
                "sha256:" + "a" * 64, ["--version"], pathlib.Path(directory)
            )
            self.assertEqual(result.stdout, "synthetic output\n")
            self.assertFalse(any("--rm" in call or "rm" in call for call in calls))
            create = next(call for call in calls if call[0] == "create")
            self.assertIn("json-file", create)
            self.assertIn("max-size=-1", create)
            self.assertTrue(
                list(
                    pathlib.Path(directory).glob(
                        "diagnostics/*/container-*/retained-container.json"
                    )
                )
            )


PREPARED = os.environ.get("RUNSPINDLE_TEMPORAL_PREPARED")


@unittest.skipUnless(
    PREPARED, "real lane not requested; set RUNSPINDLE_TEMPORAL_PREPARED"
)
class RealLaneTests(unittest.TestCase):
    def absent(self, lane):
        if getattr(lane, "retain_diagnostics", False):
            receipt = lane.verify_retained()
            self.assertEqual(
                receipt["classification"], "stopped_execution_retained_diagnostics"
            )
            self.assertEqual(
                receipt["artifacts"], {"volume": "retained", "network": "retained"}
            )
            return
        for kind, name in [
            ("container", lane.server),
            ("volume", lane.volume),
            ("network", lane.network),
        ]:
            self.assertNotEqual(
                lane_module.docker(kind, "inspect", name, check=False).returncode, 0
            )

    def test_existing_unrelated_image_cannot_replace_prepared_release(self):
        with (
            tempfile.TemporaryDirectory() as context,
            tempfile.TemporaryDirectory() as modified,
        ):
            context_path, directory = pathlib.Path(context), pathlib.Path(modified)
            (context_path / "Dockerfile").write_text(
                "FROM scratch\nLABEL synthetic.unrelated=" + uuid.uuid4().hex + "\n"
            )
            tag = "runspindle-synthetic-unrelated:" + uuid.uuid4().hex
            lane_module.docker(
                "build",
                "--load",
                "--tag",
                tag,
                "--network",
                "none",
                "--pull=false",
                "--provenance=false",
                "--iidfile",
                context_path / "image.id",
                context_path,
                timeout=60,
            )
            unrelated = lane_module.docker(
                "image", "inspect", "--format", "{{.Id}}", tag
            ).stdout.strip()
            try:
                prepared = pathlib.Path(PREPARED)
                metadata = json.loads((prepared / "prepared.json").read_text())
                metadata["image"] = unrelated
                (directory / "prepared.json").write_text(json.dumps(metadata))
                artifact = lane_module.read_pins()["cli"]["artifacts"][
                    metadata["architecture"]
                ]["filename"]
                for name in ("temporal", "LICENSE", artifact):
                    (directory / name).symlink_to(prepared / name)
                with self.assertRaisesRegex(
                    lane_module.Blocked, "image does not match"
                ):
                    lane_module.Lane(directory)
            finally:
                lane_module.docker("image", "rm", "--force", unrelated, check=False)

    def test_real_restart_persistence_and_success_cleanup(self):
        result = lane_module.smoke(PREPARED)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["workflow_events"], 2)
        expected = (
            "stopped_execution_retained_diagnostics"
            if lane_module.retention_requested()
            else "independently_absent"
        )
        self.assertEqual(result["cleanup"], expected)

    def test_exception_and_client_failure_cleanup_and_egress_denial(self):
        lane = lane_module.Lane(PREPARED)
        with self.assertRaisesRegex(RuntimeError, "synthetic fault"):
            with lane:
                # This IP is unreachable from an internal-only Docker network.
                denied = lane.cli(
                    "operator",
                    "cluster",
                    "health",
                    "--address",
                    "1.1.1.1:443",
                    "--command-timeout",
                    "1s",
                    timeout=4,
                    check=False,
                )
                self.assertNotEqual(denied.returncode, 0)
                with self.assertRaisesRegex(RuntimeError, "client failed"):
                    lane.run_client(lane.image, ["synthetic-invalid-command"])
                with self.assertRaises(subprocess.TimeoutExpired):
                    lane.run_client(
                        lane.image,
                        [
                            "--address",
                            lane.address,
                            "workflow",
                            "execute",
                            "--workflow-id",
                            "synthetic-client-deadline",
                            "--type",
                            "SyntheticUnpolled",
                            "--task-queue",
                            "synthetic-unpolled",
                        ],
                        deadline=0.2,
                    )
                self.assertEqual(lane.clients, set())
                raise RuntimeError("synthetic fault")
        self.absent(lane)

    def test_readiness_deadline_cleanup(self):
        lane = lane_module.Lane(PREPARED, readiness_deadline=0.000001)
        with self.assertRaises(lane_module.Blocked):
            with lane:
                self.fail("deadline did not block")
        self.absent(lane)

    def test_sigterm_cleanup_in_fresh_process(self):
        assert PREPARED is not None
        script = signal_child_script(PREPARED)
        controller_evidence = (
            lane_module.diagnostic_directory(
                pathlib.Path(PREPARED), "signal-controller-" + uuid.uuid4().hex
            )
            if lane_module.retention_requested()
            else None
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        names = None
        diagnostics = None
        child_stdout = ""
        child_stderr = ""
        collected = False
        line = ""
        try:
            ready, _, _ = select.select([process.stdout], [], [], 90)
            self.assertTrue(ready, "child never reached ready state")
            line = process.stdout.readline()
            self.assertTrue(line, "child exited before ready state")
            observation = json.loads(line)
            names = observation["names"]
            diagnostics = observation["diagnostics"]
            process.send_signal(signal.SIGTERM)
            child_stdout, child_stderr = process.communicate(timeout=30)
            collected = True
            self.assertNotEqual(process.returncode, 0)
            if diagnostics:
                receipts = list(
                    pathlib.Path(diagnostics).glob(
                        "container-*/retained-container.json"
                    )
                )
                self.assertTrue(receipts, "child did not retain container evidence")
                for receipt in receipts:
                    lane_module.verify_retained_container(
                        json.loads(receipt.read_text())
                    )
                for kind, name in zip(("volume", "network"), names[1:]):
                    self.assertEqual(
                        lane_module.docker(
                            kind, "inspect", name, check=False
                        ).returncode,
                        0,
                    )
            else:
                for kind, name in zip(("container", "volume", "network"), names):
                    self.assertNotEqual(
                        lane_module.docker(
                            kind, "inspect", name, check=False
                        ).returncode,
                        0,
                    )
        finally:
            if process.poll() is None:
                process.kill()
            if not collected:
                child_stdout, child_stderr = process.communicate()
            if controller_evidence is not None:
                root = controller_evidence
                lane_module.diagnostic_capture(
                    root,
                    "child-stdout-" + uuid.uuid4().hex + ".log",
                    (line + child_stdout).encode(),
                )
                lane_module.diagnostic_capture(
                    root,
                    "child-stderr-" + uuid.uuid4().hex + ".log",
                    child_stderr.encode(),
                )
            if names and not diagnostics:
                lane_module.docker("rm", "--force", names[0], check=False)
                lane_module.docker("volume", "rm", "--force", names[1], check=False)
                lane_module.docker("network", "rm", names[2], check=False)


if __name__ == "__main__":
    unittest.main()
