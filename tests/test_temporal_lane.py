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
from unittest.mock import patch

MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'tools/temporal_lane.py'
spec = importlib.util.spec_from_file_location('temporal_lane', MODULE_PATH)
lane_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane_module)


class BoundaryTests(unittest.TestCase):
    def test_floating_image_denied_before_docker(self):
        with patch.object(lane_module, 'docker') as docker:
            with self.assertRaises(lane_module.Blocked):
                lane_module.Lane._image('temporalio/temporal:latest')
            docker.assert_not_called()

    def test_corrupted_download_never_builds_or_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = lane_module.read_pins()['cli']['artifacts']['arm64']['filename']
            (pathlib.Path(directory) / artifact).write_bytes(b'synthetic-corrupt')
            with patch.object(lane_module, 'docker') as docker:
                with self.assertRaisesRegex(lane_module.Blocked, 'checksum'):
                    lane_module.prepare(directory, 'arm64')
                docker.assert_not_called()

    def test_changed_prepared_manifest_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            (pathlib.Path(directory) / 'prepared.json').write_text(json.dumps({'pins_sha256': '0'*64}))
            with self.assertRaisesRegex(lane_module.Blocked, 'manifest changed'):
                lane_module.Lane(directory)

    def test_changed_prepared_binary_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'prepared.json').write_text(json.dumps({
                'pins_sha256': lane_module.digest(lane_module.PINS), 'binary_sha256': '0'*64}))
            (root / 'temporal').write_bytes(b'synthetic-corrupt')
            with self.assertRaisesRegex(lane_module.Blocked, 'executable changed'):
                lane_module.Lane(directory)

    def test_partial_creation_lost_reply_is_cleaned(self):
        # Inject a lost response after resource allocation, at each boundary.
        for fault in ('network', 'volume', 'run'):
            with self.subTest(fault=fault):
                lane = object.__new__(lane_module.Lane)
                lane._created = False
                lane._handlers = {}
                lane.network, lane.volume, lane.server = 'synthetic-net', 'synthetic-db', 'synthetic-server'
                lane.clients = set()
                lane.image = 'sha256:' + '0'*64
                calls = []
                def fake(*args, **kwargs):
                    calls.append(args)
                    if args[0] == fault and ('create' in args or fault == 'run'):
                        raise subprocess.TimeoutExpired('synthetic-docker-lost-reply', 1)
                    return subprocess.CompletedProcess(args, 0, '', '')
                with patch.object(lane_module, 'docker', fake):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        lane.start()
                self.assertIn(('rm', '--force', lane.server), calls)
                self.assertIn(('volume', 'rm', '--force', lane.volume), calls)
                self.assertIn(('network', 'rm', lane.network), calls)

    def test_failed_client_delete_retains_cleanup_ownership(self):
        lane = object.__new__(lane_module.Lane)
        name = 'synthetic-client'
        lane.clients = {name}
        def fake(*args, **kwargs):
            if args[0] == 'rm':
                return subprocess.CompletedProcess(args, 1, '', 'synthetic transient deletion denial')
            return subprocess.CompletedProcess(args, 0, '0', '')
        with patch.object(lane, 'spawn_client', return_value=name), patch.object(lane_module, 'docker', fake):
            with self.assertRaisesRegex(RuntimeError, 'client cleanup incomplete'):
                lane.run_client('sha256:' + '0'*64, ['synthetic'])
        self.assertIn(name, lane.clients)

    def test_cleanup_failure_does_not_report_success(self):
        lane = object.__new__(lane_module.Lane)
        lane._created, lane._handlers, lane.clients = True, {}, set()
        lane.server, lane.volume, lane.network = 'server', 'volume', 'network'
        with patch.object(lane_module, 'docker', return_value=subprocess.CompletedProcess([], 1, '', 'synthetic daemon unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'cleanup incomplete'):
                lane.close()


PREPARED = os.environ.get('RUNSPINDLE_TEMPORAL_PREPARED')


@unittest.skipUnless(PREPARED, 'real lane not requested; set RUNSPINDLE_TEMPORAL_PREPARED')
class RealLaneTests(unittest.TestCase):
    def absent(self, lane):
        for kind, name in [('container', lane.server), ('volume', lane.volume), ('network', lane.network)]:
            self.assertNotEqual(lane_module.docker(kind, 'inspect', name, check=False).returncode, 0)

    def test_existing_unrelated_image_cannot_replace_prepared_release(self):
        with tempfile.TemporaryDirectory() as context, tempfile.TemporaryDirectory() as modified:
            context_path, directory = pathlib.Path(context), pathlib.Path(modified)
            (context_path / 'Dockerfile').write_text('FROM scratch\nLABEL synthetic.unrelated=' + uuid.uuid4().hex + '\n')
            lane_module.docker('build', '--network', 'none', '--pull=false', '--provenance=false',
                               '--iidfile', context_path / 'image.id', context_path, timeout=60)
            unrelated = (context_path / 'image.id').read_text().strip()
            try:
                prepared = pathlib.Path(PREPARED)
                metadata = json.loads((prepared / 'prepared.json').read_text())
                metadata['image'] = unrelated
                (directory / 'prepared.json').write_text(json.dumps(metadata))
                artifact = lane_module.read_pins()['cli']['artifacts'][metadata['architecture']]['filename']
                for name in ('temporal', 'LICENSE', artifact):
                    (directory / name).symlink_to(prepared / name)
                with self.assertRaisesRegex(lane_module.Blocked, 'image does not match'):
                    lane_module.Lane(directory)
            finally:
                lane_module.docker('image', 'rm', '--force', unrelated, check=False)

    def test_real_restart_persistence_and_success_cleanup(self):
        result = lane_module.smoke(PREPARED)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['workflow_events'], 2)
        self.assertEqual(result['cleanup'], 'independently_absent')

    def test_exception_and_client_failure_cleanup_and_egress_denial(self):
        lane = lane_module.Lane(PREPARED)
        with self.assertRaisesRegex(RuntimeError, 'synthetic fault'):
            with lane:
                # This IP is unreachable from an internal-only Docker network.
                denied = lane.cli('operator', 'cluster', 'health', '--address', '1.1.1.1:443',
                                  '--command-timeout', '1s', timeout=4, check=False)
                self.assertNotEqual(denied.returncode, 0)
                with self.assertRaisesRegex(RuntimeError, 'client failed'):
                    lane.run_client(lane.image, ['synthetic-invalid-command'])
                with self.assertRaises(subprocess.TimeoutExpired):
                    lane.run_client(lane.image, ['--address', lane.address, 'workflow', 'execute',
                                    '--workflow-id', 'synthetic-client-deadline', '--type',
                                    'SyntheticUnpolled', '--task-queue', 'synthetic-unpolled'], deadline=0.2)
                self.assertEqual(lane.clients, set())
                raise RuntimeError('synthetic fault')
        self.absent(lane)

    def test_readiness_deadline_cleanup(self):
        lane = lane_module.Lane(PREPARED, readiness_deadline=0.000001)
        with self.assertRaises(lane_module.Blocked):
            with lane:
                self.fail('deadline did not block')
        self.absent(lane)

    def test_sigterm_cleanup_in_fresh_process(self):
        script = f'''import importlib.util,json,time
s=importlib.util.spec_from_file_location('lane',{str(MODULE_PATH)!r})
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
with m.Lane({PREPARED!r}) as lane:
 print(json.dumps([lane.server,lane.volume,lane.network]),flush=True)
 time.sleep(120)
'''
        process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        names = None
        try:
            ready, _, _ = select.select([process.stdout], [], [], 90)
            self.assertTrue(ready, 'child never reached ready state')
            line = process.stdout.readline()
            self.assertTrue(line, 'child exited before ready state')
            names = json.loads(line)
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=30)
            self.assertNotEqual(process.returncode, 0)
            for kind, name in zip(('container', 'volume', 'network'), names):
                self.assertNotEqual(lane_module.docker(kind, 'inspect', name, check=False).returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            if names:
                lane_module.docker('rm', '--force', names[0], check=False)
                lane_module.docker('volume', 'rm', '--force', names[1], check=False)
                lane_module.docker('network', 'rm', names[2], check=False)


if __name__ == '__main__':
    unittest.main()
