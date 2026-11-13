"""Preserve diagnostics across failed administration and wait for log flushing."""
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from runner_fleet.backend import Backend, execute
from runner_fleet.manager import Manager
from runner_fleet.model import config
from runner_fleet.state import State


class DiagnosticsTests(unittest.TestCase):
    def test_unbounded_guest_output_fails_closed_before_host_exhaustion(self):
        with self.assertRaisesRegex(RuntimeError, 'output limit'):
            execute([sys.executable, '-c', "import sys; sys.stdout.write('x' * 1000000)"], output_limit=4096)
        result = execute([sys.executable, '-c', "print('small diagnostic')"], output_limit=4096)
        self.assertEqual(result.stdout.strip(), 'small diagnostic')

    def test_failed_ssh_does_not_overwrite_captured_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = State(Path(temporary) / 'state')
            resource = state.reserve({'repository': 'runspindle-platform', 'id': 1})
            backend = Backend(state)
            path = backend.directory(resource) / 'guest-diagnostics.log'
            path.write_text('earlier valid diagnostics')
            with patch.object(backend, 'slot', return_value=Path(temporary)), patch.object(backend, 'ssh', return_value=subprocess.CompletedProcess([], 255, '', 'guest stopped')), patch('runner_fleet.backend.execute'):
                backend.diagnostics(resource)
            self.assertEqual(path.read_text(), 'earlier valid diagnostics')
            self.assertIn('guest stopped', (path.parent / 'diagnostic-errors.log').read_text())

    def test_completed_job_waits_before_guest_destruction(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = Mock()
            manager = Manager(config(), temporary, backend)
            resource = manager.state.reserve({'repository': 'runspindle-platform', 'id': 1})
            manager.state.update(resource, 'running', registered_at=time.time())
            resource = manager.state.active()[0]
            job = {'id': 2, 'runner_name': resource['runner_name'], 'status': 'completed', 'conclusion': 'success'}
            manager.advance(resource, [job])
            resource = manager.state.active()[0]
            self.assertEqual(resource['phase'], 'finishing')
            self.assertLessEqual(resource['details']['finish_deadline'] - time.time(), 120)
            backend.destroy.assert_not_called()
            backend.runner_exited.return_value = False
            manager.advance(resource, [])
            backend.destroy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
