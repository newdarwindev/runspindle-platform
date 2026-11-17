"""Synthetic probe failure tests; no VM, registration, or host network changes."""

import base64
import fcntl
import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.runner_fleet import offline_probe as probe


class OfflineProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def result(self, **changes: Any) -> dict[str, Any]:
        value = {
            name: {"returncode": 0, "version": version}
            for name, version in probe.EXPECTED.items()
        }
        value.update(changes)
        return value

    def serial(self, payload: dict[str, Any], count: int = 1) -> Path:
        path = self.root / "serial"
        path.write_bytes(
            (b"RUNSPINDLE-OFFLINE-nonce:" + json.dumps(payload).encode() + b"\n")
            * count
        )
        return path

    def test_exact_versions_required(self) -> None:
        self.assertEqual(
            probe.parse_result(self.serial(self.result()), "nonce"), self.result()
        )
        for version in ["cargo 1.94.10", "cargo 1.93.0", "cargo 1.94.1\nextra", None]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                probe.parse_result(
                    self.serial(
                        self.result(cargo={"version": version, "returncode": 0})
                    ),
                    "nonce",
                )

    def test_missing_duplicate_wrong_nonce_and_nonzero_refused(self) -> None:
        for count in (0, 2):
            with self.assertRaises(ValueError):
                probe.parse_result(self.serial(self.result(), count), "nonce")
        with self.assertRaises(ValueError):
            probe.parse_result(self.serial(self.result()), "other")
        with self.assertRaises(ValueError):
            probe.parse_result(
                self.serial(
                    self.result(ruff={"version": "ruff 0.14.5", "returncode": 1})
                ),
                "nonce",
            )

    def test_overlong_output_refused(self) -> None:
        path = self.root / "serial"
        path.write_bytes(b"x" * 65537)
        with self.assertRaises(ValueError):
            probe.parse_result(path, "nonce")

    def test_no_network_devices_helpers_or_host_mounts(self) -> None:
        args = probe.qemu_args(self.root)
        self.assertEqual(args[args.index("-nic") + 1], "none")
        for forbidden in ("-netdev", "-net", "-virtfs", "-fsdev", "-daemonize", "-qmp"):
            self.assertNotIn(forbidden, args)
        self.assertIn("-no-user-config", args)
        self.assertIn("-nodefaults", args)
        self.assertIn("resourcecontrol=deny", args[-1])

    def test_guest_has_fixed_no_install_commands(self) -> None:
        config = json.loads(probe.guest_config("x").split(b"\n", 1)[1])
        script = base64.b64decode(config["write_files"][0]["content"]).decode()
        self.assertIn("/opt/runspindle-quality/bin/ruff", script)
        self.assertIn("/opt/rust/bin/cargo", script)
        self.assertNotIn("curl", script)
        self.assertNotIn("pip install", script)
        compile(script, "<guest-script>", "exec")

    def test_metadata_rejects_backing_dirty_external_and_large(self) -> None:
        valid = {
            "format": "qcow2",
            "virtual-size": 24 * 1024**3,
            "format-specific": {"data": {}},
        }
        path = self.root / "info"
        path.write_text(json.dumps(valid))
        self.assertEqual(probe.image_info(path), valid)
        changes: dict[str, Any]
        for changes in (
            {"backing-filename": "other"},
            {"dirty-flag": True},
            {"format": "raw"},
            {"virtual-size": probe.MAX_IMAGE + 1},
            {"snapshots": []},
            {"format-specific": {"data": {"data-file": "outside"}}},
        ):
            path.write_text(json.dumps(valid | changes))
            with self.assertRaises(ValueError):
                probe.image_info(path)

    def test_parent_requires_private_canonical_directory(self) -> None:
        self.assertEqual(probe.parent_path(self.root), self.root)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            probe.parent_path(link)
        self.root.chmod(0o755)
        with self.assertRaises(ValueError):
            probe.parent_path(self.root)
        self.root.chmod(0o700)

    def test_image_requires_sealed_root_owned_file(self) -> None:
        path = self.root / "image"
        path.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            probe.image_path(path)

    def test_capture_never_overwrites(self) -> None:
        path = self.root / "log"
        probe.write(path, b"original")
        with self.assertRaises(FileExistsError):
            probe.write(path, b"new")
        self.assertEqual(path.read_bytes(), b"original")

    def test_allocator_bound_preserves_hard_memory_limit_and_private_env(self) -> None:
        with patch.dict(os.environ, {"SYNTHETIC_PRIVATE_VALUE": "must-not-pass"}):
            result = probe.command(
                [
                    sys.executable,
                    "-c",
                    "import json,os,resource; print(json.dumps([os.environ.get('MALLOC_ARENA_MAX'),resource.getrlimit(resource.RLIMIT_AS),os.environ.get('SYNTHETIC_PRIVATE_VALUE')]))",
                ],
                self.root,
                "allocator",
                time.monotonic() + 10,
            )
        self.assertEqual(
            json.loads(result.read_text()), ["2", [4 * 1024**3, 4 * 1024**3], None]
        )

    def test_command_failure_preserves_output(self) -> None:
        with self.assertRaises(RuntimeError):
            probe.command(
                [
                    sys.executable,
                    "-c",
                    'print("full output", flush=True); raise SystemExit(2)',
                ],
                self.root,
                "failure",
                time.monotonic() + 10,
            )
        self.assertEqual((self.root / "failure.log").read_text(), "full output\n")

    def test_timeout_kills_process_and_preserves_prefix(self) -> None:
        with self.assertRaises(RuntimeError):
            probe.command(
                [
                    sys.executable,
                    "-c",
                    'import time; print("prefix", flush=True); time.sleep(30)',
                ],
                self.root,
                "timeout",
                time.monotonic() + 0.2,
            )
        self.assertEqual((self.root / "timeout.log").read_text(), "prefix\n")

    def test_size_limit_is_enforced_by_kernel(self) -> None:
        def small_limit() -> None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))

        with (
            patch.object(probe, "limits", small_limit),
            self.assertRaises(RuntimeError),
        ):
            probe.command(
                [
                    sys.executable,
                    "-c",
                    'import os; os.write(1, b"x" * 8192); os.write(1,b"y")',
                ],
                self.root,
                "limit",
                time.monotonic() + 10,
            )
        self.assertEqual((self.root / "limit.log").stat().st_size, 4096)

    def test_parent_sigkill_still_has_independent_deadline(self) -> None:
        code = "from pathlib import Path; import sys,time; from tools.runner_fleet.offline_probe import command; command([sys.executable,'-c','import os,time; print(os.getpid(),flush=True); time.sleep(30)'],Path(sys.argv[1]),'orphan',time.monotonic()+1)"
        parent = subprocess.Popen(
            [sys.executable, "-c", code, str(self.root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        path = self.root / "orphan.log"
        try:
            until = time.monotonic() + 2
            while time.monotonic() < until and (
                not path.exists() or not path.read_text().strip()
            ):
                time.sleep(0.01)
            child_pid = int(path.read_text().strip())
            parent.kill()
            parent.wait()
            until = time.monotonic() + 3
            while time.monotonic() < until:
                state = Path("/proc") / str(child_pid) / "stat"
                if not state.exists() or state.read_text().split()[2] == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail("Child survived independent deadline")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_parent_kill_keeps_lock_until_supervised_child_exits(self) -> None:
        lock_path = self.root / "shared.lock"
        code = "from pathlib import Path; import sys,time,fcntl; from tools.runner_fleet.offline_probe import command; root=Path(sys.argv[1]); lock=open(root/'shared.lock','w'); fcntl.flock(lock,fcntl.LOCK_EX); command([sys.executable,'-c','import os,time; print(os.getpid(),flush=True); time.sleep(30)'],root,'locked',time.monotonic()+1,pass_fds=(lock.fileno(),))"
        parent = subprocess.Popen(
            [sys.executable, "-c", code, str(self.root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        output = self.root / "locked.log"
        try:
            until = time.monotonic() + 2
            while time.monotonic() < until and (
                not output.exists() or not output.read_text().strip()
            ):
                time.sleep(0.01)
            self.assertTrue(output.read_text().strip())
            parent.kill()
            parent.wait()
            with lock_path.open("r+") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                until = time.monotonic() + 3
                while time.monotonic() < until:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.02)
                else:
                    self.fail("Lock did not release after supervisor deadline")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_bad_digest_has_no_execution_effect(self) -> None:
        image = self.root / "image"
        image.write_bytes(b"content")
        with (
            patch.object(probe, "image_path", return_value=image),
            patch.object(probe, "command") as command,
        ):
            with self.assertRaises(ValueError):
                probe.probe(image, "0" * 64, self.root)
            command.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["image"])

    def test_low_memory_still_blocks_real_admission(self) -> None:
        image = self.root / "image"
        image.write_bytes(b"content")
        with (
            patch.object(probe, "image_path", return_value=image),
            patch("os.access", return_value=True),
            patch.object(probe, "MIN_FREE", 0),
            patch.object(probe, "available_memory", return_value=1024**3),
            patch.object(probe, "command") as command,
            self.assertRaisesRegex(RuntimeError, "11 GiB"),
        ):
            probe.probe(image, hashlib.sha256(b"content").hexdigest(), self.root)
        command.assert_not_called()

    def test_failure_receipt_preserves_artifacts_and_no_qualification(self) -> None:
        image = self.root / "image"
        image.write_bytes(b"content")
        with (
            patch.object(probe, "image_path", return_value=image),
            patch("os.access", return_value=True),
            patch.object(probe, "MIN_FREE", 0),
            patch.object(probe, "available_memory", return_value=12 * 1024**3),
            patch.object(probe, "command", side_effect=RuntimeError("synthetic stop")),
        ):
            result = probe.probe(
                image, hashlib.sha256(b"content").hexdigest(), self.root
            )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ci_qualified"])
        self.assertFalse(result["promoted"])
        self.assertFalse(result["network_tested"])
        retained = Path(result["evidence_directory"]) / "result.json"
        self.assertEqual(json.loads(retained.read_text()), result)
        self.assertEqual(image.read_bytes(), b"content")


if __name__ == "__main__":
    unittest.main()
