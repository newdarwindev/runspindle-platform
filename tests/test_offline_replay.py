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
