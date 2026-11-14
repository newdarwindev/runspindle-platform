"""A stale cleanup request must never affect a replacement guest."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from runner_fleet import host_helper


class OwnershipTests(unittest.TestCase):
    def test_stale_stop_rejected_before_external_operation(self):
        with patch.object(host_helper, 'observe', return_value={'resource_id': 'b'*32}), patch.object(host_helper, 'run') as external:
            with self.assertRaisesRegex(RuntimeError, 'ownership mismatch'):
                host_helper.stop(0, 'a'*32)
            external.assert_not_called()


if __name__ == '__main__':
    unittest.main()
