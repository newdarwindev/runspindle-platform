"""Independent observations for capacity and exact-image identity reporting."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from runner_fleet.lifecycle import capacity_snapshot, image_matrix
from runner_fleet.model import config
from runner_fleet.state import State


class LifecycleTests(unittest.TestCase):
    def state(self, directory):
        state = State(directory)
        state.set('heartbeat', 100)
        state.set('poll_at', 100)
        state.set('drained', False)
        return state

    def guests(self):
        return [{'slot': slot, 'ActiveState': 'inactive', 'MainPID': '0'} for slot in range(2)]

    def snapshot(self, state, guests=None, now=101):
        return capacity_snapshot(state, guests if guests is not None else self.guests(),
                                 {'ActiveState': 'active'}, now)

    def test_fresh_capacity_and_stale_or_future_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory)
            self.assertEqual(self.snapshot(state)['available_slots'], 2)
            for key in ('heartbeat', 'poll_at'):
                for at in (None, -100, 220, '100'):
                    state.set(key, at)
                    result = self.snapshot(state)
                    self.assertEqual(result['status'], 'blocked')
                    self.assertEqual(result['available_slots'], 0)
                state.set(key, 100)
            state.set('drained', True)
            self.assertEqual(self.snapshot(state)['available_slots'], 0)

    def test_unknown_and_cleaning_resources_keep_slots_after_fresh_process(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory)
            requested = {'repository': 'runspindle-platform', 'id': 55,
                         'head_sha': 'a' * 40, 'run_id': 7, 'event': 'push', 'head_branch': 'ci/synthetic'}
            first = state.reserve(requested)
            state.update(first, 'uncertain', image_sha256='b' * 64)
            second = state.reserve(requested | {'id': 56})
            state.update(second, 'cleaning')
            state.db.close()
            script = '''import json,sys
from runner_fleet.state import State
from runner_fleet.lifecycle import capacity_snapshot
s=State(sys.argv[1])
print(json.dumps({'resources':s.active(), 'capacity':capacity_snapshot(s,[{'slot':i,'ActiveState':'inactive','MainPID':'0'} for i in range(2)],{'ActiveState':'active'},101)}))
'''
            import os
            result = subprocess.run([sys.executable, '-c', script, directory], check=True,
                                    capture_output=True, text=True,
                                    env=dict(os.environ, PYTHONPATH=str(ROOT / 'tools')))
            observed = json.loads(result.stdout)
            self.assertEqual(observed['capacity']['available_slots'], 0)
            self.assertEqual(observed['capacity']['occupied_slots'], [0, 1])
            source = observed['resources'][0]['details']['requested_source']
            self.assertEqual(source['head_sha'], requested['head_sha'])
            self.assertEqual(source['run_id'], 7)
            self.assertNotIn('claimed_job', observed['resources'][0]['details'])

    def test_guest_loss_foreign_guest_and_missing_observation_block_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(directory)
            resource = state.reserve({'repository': 'synthetic', 'id': 1})
            state.update(resource, 'running')
            guests = self.guests()
            self.assertEqual(self.snapshot(state)['status'], 'blocked')
            guests[0].update(ActiveState='active', resource_id='foreign')
            self.assertEqual(self.snapshot(state, guests)['available_slots'], 0)
            guests[0]['resource_id'] = resource['id']
            self.assertEqual(self.snapshot(state, guests)['available_slots'], 1)
            for observed in (None, {}, [None, None], guests[:1], guests + [guests[0]], [guests[0], {'slot': 1}]):
                self.assertEqual(capacity_snapshot(state, observed, {'ActiveState': 'active'}, 101)['available_slots'], 0)
            guests[1]['MainPID'] = '99'
            self.assertEqual(self.snapshot(state, guests)['available_slots'], 0)
            guests[1]['MainPID'] = '0'
            state.set('error', 'inventory unavailable')
            self.assertEqual(self.snapshot(state, guests)['available_slots'], 0)

    def test_exact_image_and_recipe_are_identity_only_and_mutations_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'synthetic.img'
            manifest = root / 'manifest.json'
            image.write_bytes(b'synthetic image bytes')
            inputs = ROOT / 'fleet/image-inputs.json'
            record = {'sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                      'inputs': json.loads(inputs.read_text())}
            manifest.write_text(json.dumps(record))
            result = image_matrix(config(), inputs, image, manifest)
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(result['lanes'][0]['identity_status'], 'passed')
            self.assertTrue(all(row['qualification'] == 'blocked' for row in result['lanes']))
            image.write_bytes(b'changed bytes')
            self.assertIn('image digest mismatch', image_matrix(config(), inputs, image, manifest)['lanes'][0]['identity_reasons'])
            record['sha256'] = hashlib.sha256(image.read_bytes()).hexdigest()
            record['inputs']['rust']['version'] = 'floating'
            manifest.write_text(json.dumps(record))
            self.assertIn('image recipe mismatch', image_matrix(config(), inputs, image, manifest)['lanes'][0]['identity_reasons'])
            manifest.unlink()
            self.assertEqual(image_matrix(config(), inputs, image, manifest)['lanes'][0]['identity_status'], 'blocked')
            for malformed in ('[]', 'null', '{'):
                manifest.write_text(malformed)
                self.assertEqual(image_matrix(config(), inputs, image, manifest)['lanes'][0]['identity_status'], 'blocked')

    def test_read_only_image_cli_does_not_create_state_or_launch_helpers(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'must-not-exist'
            result = subprocess.run([sys.executable, str(ROOT / 'tools/fleet.py'), 'image-status',
                                     '--host', 'strix', '--state-dir', str(state),
                                     '--image', str(state / 'missing'), '--image-manifest', str(state / 'missing.json')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['status'], 'blocked')
            self.assertFalse(state.exists())


if __name__ == '__main__':
    unittest.main()
