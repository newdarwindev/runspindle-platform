from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tools.runner_fleet.ephemeral_registration import configure, start_listener
from tools.runner_fleet.private_relay import RelayError


class EphemeralRegistrationTest(unittest.TestCase):
    def test_configures_only_the_exact_repository_and_leaves_listener_offline(self):
        now = datetime.now(timezone.utc)
        for repository, label in (
            ("newdarwindev/runspindle-host", "runspindle-strix-direct-123-1-component"),
            ("newdarwindev/runspindle-strix-ci-canary", "runspindle-strix-canary-proof-123-1"),
        ):
            with self.subTest(repository=repository):
                name = "runspindle-fleet-" + "f" * 32
                token = "synthetic-test-credential"
                api = Mock(side_effect=[
                    {"id": 500, "full_name": repository, "private": repository.endswith("canary")},
                    {"token": token, "expires_at": (now + timedelta(minutes=20)).isoformat()},
                    {"total_count": 1, "runners": [{
                        "id": 10, "name": name, "ephemeral": True, "busy": False,
                        "status": "offline", "labels": [{"name": label, "type": "custom"}],
                    }]},
                ])
                ssh = Mock(return_value=SimpleNamespace(returncode=0))
                admit = Mock()
                self.assertEqual(
                    configure(api, ssh, repository, 500, name, label, admit=admit, clock=lambda: now),
                    10,
                )
                self.assertEqual(admit.call_count, 2)
                script, = ssh.call_args.args
                self.assertIn(f"ACTIONS_RUNNER_INPUT_TOKEN={token} ", script)
                self.assertNotIn("--token", script)
                self.assertIn("--no-default-labels", script)
                self.assertNotIn("Listener run", script)
                self.assertEqual(api.call_args_list[1].args, (
                    "POST", f"/repos/{repository}/actions/runners/registration-token", 201,
                ))
                start_listener(ssh, admit=admit)
                self.assertNotIn(token, ssh.call_args.args[0])
                self.assertIn("Listener run", ssh.call_args.args[0])

    def test_rejects_generic_and_cross_scope_labels_before_api(self):
        api = Mock()
        for label in ("self-hosted", "runspindle-strix-canary-proof-123-1"):
            with self.assertRaises(RelayError):
                configure(
                    api, Mock(), "newdarwindev/runspindle-host", 500,
                    "runspindle-fleet-" + "f" * 32, label, admit=Mock(),
                )
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
