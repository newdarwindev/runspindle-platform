"""A missing dispatch must stop at the queue limit, without synthetic success."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from runner_fleet.model import config
from runner_fleet.service import qualify


class QualificationTests(unittest.TestCase):
    def test_unobserved_dispatch_expires_at_queue_not_execution_deadline(self):
        cfg = config()
        cfg['queue_timeout_seconds'] = 1
        sha = 'a'*40
        with tempfile.TemporaryDirectory() as directory, patch('runner_fleet.service.gh', side_effect=[{'sha':sha}, {'workflow_runs':[]}, {'workflow_runs':[]}]), patch('runner_fleet.service.execute', return_value=subprocess.CompletedProcess([],0,'')), patch('runner_fleet.service.time.monotonic', side_effect=[0,2,2,2]), patch('runner_fleet.service.time.sleep', side_effect=AssertionError('waited beyond queue deadline')):
            report = qualify(cfg, {'runspindle-platform':sha}, Path(directory), 'ci/synthetic')
        cell = next(c for c in report['coverage'] if c['repository'].endswith('/runspindle-platform') and c['lane']=='strix-linux-x64')
        self.assertEqual(cell['status'], 'blocked')
        self.assertIn('queue deadline', cell['reason'])
        self.assertEqual(report['workload_status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
