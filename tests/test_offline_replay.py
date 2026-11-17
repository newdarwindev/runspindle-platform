import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools.runner_fleet import offline_probe, offline_replay


class ReplayTests(unittest.TestCase):
    def test_bundle_is_bounded_and_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "source.bundle"
            data = b"# v2 git bundle\nsynthetic"
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(offline_replay.load_bundle(path, digest, "a" * 40), data)
            for expected, source in [("f" * 64, "a" * 40), (digest, "no")]:
                with self.assertRaises(ValueError):
                    offline_replay.load_bundle(path, expected, source)
            alias = Path(d) / "alias"
            alias.symlink_to(path)
            with self.assertRaises(ValueError):
                offline_replay.load_bundle(alias, digest, "a" * 40)
            path.write_bytes(b"x" * (offline_replay.MAX_BUNDLE + 1))
            with self.assertRaises(ValueError):
                offline_replay.load_bundle(path, digest, "a" * 40)

    def test_config_is_fixed_and_offline(self) -> None:
        import base64

        config = json.loads(
            offline_replay.guest_config("n", b"bundle", "a" * 40).split(b"\n", 1)[1]
        )
        script = base64.b64decode(config["write_files"][1]["content"]).decode()
        compile(script, "<guest>", "exec")
        self.assertIn("/dev/virtio-ports/org.runspindle.replay", script)
        self.assertNotIn("/dev/ttyS0", script)
        self.assertIn("core.hooksPath", script)
        self.assertIn("workflow mismatch", script)
        self.assertIn("tools/validate.py", script)
        self.assertNotIn("GITHUB_RUN_ID", script)
        self.assertNotIn("https://", script)
        args = offline_probe.qemu_args(Path("/fixture"))
        self.assertEqual(args[args.index("-nic") + 1], "none")

    def test_results_fail_closed(self) -> None:
        source = "a" * 40
        value = {
            "source_sha": source,
            "workflow_sha256": offline_replay.WORKFLOW_SHA256,
            "setup_ok": True,
            "commands": [
                {"argv": argv, "returncode": 0} for argv in offline_replay.COMMANDS
            ],
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "guest.log"

            def save(item: dict[str, Any]) -> None:
                path.write_text("RUNSPINDLE-REPLAY-n:" + json.dumps(item) + "\n")

            save(value)
            self.assertEqual(offline_replay.parse_result(path, "n", source), value)
            with self.assertRaises(TimeoutError):
                offline_replay.parse_result(path, "n", source, 0)
            save(value | {"workflow_sha256": "wrong"})
            with self.assertRaises(ValueError):
                offline_replay.parse_result(path, "n", source)
            save(value | {"setup_ok": False})
            with self.assertRaises(ValueError):
                offline_replay.parse_result(path, "n", source)
            save(value | {"commands": []})
            with self.assertRaises(ValueError):
                offline_replay.parse_result(path, "n", source)
            save(value)
            path.write_text(path.read_text() * 2)
            with self.assertRaises(ValueError):
                offline_replay.parse_result(path, "n", source)

    def test_partial_replay_pins_rejected_before_effects(self) -> None:
        with self.assertRaises(ValueError):
            offline_probe.probe(
                Path("/none"), "a" * 64, Path("/none"), source_sha="a" * 40
            )

    def test_writer_budget_preserves_reserve(self) -> None:
        self.assertGreaterEqual(
            offline_probe.REPLAY_MIN_FREE,
            30 * 1024**3 + sum(offline_probe.REPLAY_WRITERS.values()),
        )
        self.assertEqual(offline_probe.REPLAY_WRITERS["disk.qcow2"], 2 * 1024**3)
        self.assertEqual(offline_probe.REPLAY_WRITERS["guest.log"], 2 * 1024**3)
        self.assertEqual(offline_probe.FILE_LIMIT, 512 * 1024**2)

    def test_replay_kernel_caps_and_sparse_overflow(self) -> None:
        import sys
        import time

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = offline_probe.command(
                [
                    sys.executable,
                    "-c",
                    "import json,resource; print(json.dumps([resource.getrlimit(resource.RLIMIT_FSIZE),resource.getrlimit(resource.RLIMIT_AS),resource.getrlimit(resource.RLIMIT_CPU),resource.getrlimit(resource.RLIMIT_CORE)]))",
                ],
                root,
                "caps",
                time.monotonic() + 10,
                replay_writer=True,
            )
            self.assertEqual(
                json.loads(path.read_text()),
                [[2 * 1024**3] * 2, [4 * 1024**3] * 2, [240] * 2, [0] * 2],
            )
            code = 'import os,sys; f=open(sys.argv[1],"wb"); f.seek(2*1024**3-1); f.write(b"x"); f.flush(); f.write(b"y"); f.flush()'
            with self.assertRaises(RuntimeError):
                offline_probe.command(
                    [sys.executable, "-c", code, str(root / "sparse")],
                    root,
                    "overflow",
                    time.monotonic() + 10,
                    replay_writer=True,
                )
            self.assertEqual((root / "sparse").stat().st_size, 2 * 1024**3)

    def test_dedicated_transport_is_non_network_and_budgeted(self) -> None:
        args = offline_probe.qemu_args(Path("/fixture"), replay_output=True)
        self.assertEqual(args[args.index("-nic") + 1], "none")
        self.assertIn("file,id=replay,path=/fixture/replay.log,append=on", args)
        self.assertIn("virtserialport,chardev=replay,name=org.runspindle.replay", args)
        self.assertEqual(offline_probe.REPLAY_WRITERS["replay.log"], 2 * 1024**3)
        self.assertEqual(offline_probe.REPLAY_MIN_FREE, 39 * 1024**3)
        for forbidden in ("-netdev", "-virtfs", "-fsdev", "-qmp", "-daemonize"):
            self.assertNotIn(forbidden, args)
