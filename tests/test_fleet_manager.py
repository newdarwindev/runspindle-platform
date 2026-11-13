"""Independent failure injection at VM/registration/cleanup boundaries."""
from pathlib import Path
import tempfile
import sys
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from runner_fleet.manager import Manager, trusted
from runner_fleet.model import config


class ManagerTests(unittest.TestCase):
    def manager(self, directory):
        backend = Mock()
        backend.directory.return_value = Path(directory)
        manager = Manager(config(), directory, backend)
        return manager, backend

    def resource(self, manager, phase, **details):
        resource = manager.state.reserve({'repository': 'runspindle-platform', 'id': 44})
        manager.state.update(resource, phase, **details)
        return manager.state.active()[0]

    def test_failed_isolation_never_requests_registration_token(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self.manager(directory)
            resource = self.resource(manager, 'starting', boot_deadline=time.time()+30)
            backend.ready.return_value = True
            backend.isolation.side_effect = RuntimeError('canary reachable')
            with patch('runner_fleet.manager.gh') as api:
                manager.advance(resource, [])
                api.assert_not_called()
            backend.register.assert_not_called()
            self.assertEqual(manager.state.active()[0]['phase'], 'uncertain')

    def test_failed_cleanup_preserves_capacity_until_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self.manager(directory)
            resource = self.resource(manager, 'running', registered_at=time.time())
            backend.destroy.side_effect = RuntimeError('lost stop reply')
            with patch('runner_fleet.manager.helper', return_value={'ActiveState': 'active'}):
                manager.cleanup(resource, 'synthetic cancellation')
            self.assertEqual(len(manager.state.active()), 1)
            self.assertEqual(manager.state.active()[0]['phase'], 'uncertain')
            backend.destroy.side_effect = None
            with patch.object(manager, 'remove_registration'), patch('runner_fleet.manager.helper', return_value={'ActiveState': 'inactive'}):
                manager.cleanup(manager.state.active()[0], 'reconcile')
            self.assertEqual(manager.state.active(), [])

    def test_actual_claimed_job_not_requested_job_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self.manager(directory)
            resource = self.resource(manager, 'running', registered_at=time.time())
            claimed = {'id': 999, 'runner_name': resource['runner_name'], 'status': 'in_progress'}
            with patch('runner_fleet.manager.helper', return_value={'ActiveState': 'active'}):
                manager.advance(resource, [claimed])
            self.assertEqual(manager.state.active()[0]['requested_job'], 44)
            self.assertEqual(manager.state.active()[0]['details']['claimed_job']['id'], 999)

    def test_execution_deadline_cancels_and_destroys(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self.manager(directory)
            resource = self.resource(manager, 'running', registered_at=time.time()-901)
            job = {'id': 999, 'runner_name': resource['runner_name'], 'status': 'in_progress',
                   'started_at': '2000-01-01T00:00:00Z', 'run_id': 101}
            with patch('runner_fleet.manager.gh') as api, patch('runner_fleet.manager.helper', return_value={'ActiveState': 'active'}), patch.object(manager, 'remove_registration'):
                manager.advance(resource, [job])
                self.assertTrue(api.call_args.args[0].endswith('/101/cancel'))
            backend.destroy.assert_called_once()
            self.assertEqual(manager.state.active(), [])

    def test_drain_and_boot_error_do_not_release_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, backend = self.manager(directory)
            job = {'repository': 'runspindle-platform', 'id': 1,
                   'labels': config()['lanes']['strix-linux-x64']['labels']}
            with patch.object(manager, 'inventory', return_value=([job], [])):
                manager.tick()  # default drained
                backend.create.assert_not_called()
                manager.state.set('drained', False)
                backend.create.side_effect = RuntimeError('lost boot reply')
                manager.tick()
                self.assertEqual(manager.state.active()[0]['phase'], 'uncertain')
                self.assertEqual(manager.state.get('peak_slots'), 1)

    def test_only_same_repository_main_or_ci_push_and_dispatch_are_trusted(self):
        full = 'newdarwindev/runspindle-platform'
        run = {'event': 'push', 'head_branch': 'ci/probe', 'head_repository': {'full_name': full}}
        self.assertTrue(trusted(run, full))
        for change in ({'event': 'pull_request'}, {'head_branch': 'feature/untrusted'},
                       {'head_repository': {'full_name': 'fork/platform'}}):
            self.assertFalse(trusted(run | change, full))


if __name__ == '__main__':
    unittest.main()
