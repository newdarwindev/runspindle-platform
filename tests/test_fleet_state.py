"""Fault boundaries for durable allocation and proxy DNS rebinding."""
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from runner_fleet.state import State
from runner_fleet.proxy import destination


class DurableTests(unittest.TestCase):
    def test_restart_unknown_cleanup_capacity_and_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            first = state.reserve({'repository': 'synthetic', 'id': 1})
            state.update(first, 'uncertain', error='lost boot reply')
            second = state.reserve({'repository': 'other', 'id': 2})
            state.update(second, 'cleaning')
            state.db.close()
            state = State(directory)
            with self.assertRaises(RuntimeError):
                state.reserve({'repository': 'third', 'id': 3})
            state.update(first, 'destroyed', cleanup_verified=True)
            third = state.reserve({'repository': 'third', 'id': 3})
            self.assertEqual(third['slot'], first['slot'])
            with self.assertRaises(sqlite3.IntegrityError):
                state.db.execute('INSERT INTO resources SELECT "duplicate",slot,repository,requested_job+1,"duplicate",phase,created,updated,details FROM resources WHERE id=?', (third['id'],))
            state.db.rollback()

    def test_exclusive_process_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with State(directory).lock():
                with self.assertRaises(BlockingIOError):
                    with State(directory).lock():
                        self.fail('second manager admitted')

    def test_duplicate_job_reservation_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(directory)
            state.reserve({'repository': 'synthetic', 'id': 1})
            with self.assertRaises(sqlite3.IntegrityError):
                state.reserve({'repository': 'synthetic', 'id': 1})
            self.assertEqual(len(state.active()), 1)


class EgressTests(unittest.TestCase):
    def answer(self, *addresses):
        return lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addresses]

    def test_deny_private_reserved_mixed_answers_and_suffix_confusion(self):
        policy = ['github.com', '*.actions.githubusercontent.com']
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '192.168.1.1', '198.18.0.2', '224.0.0.1', '0.0.0.0'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                destination('github.com:443', policy, self.answer('140.82.112.3', address))
        for authority in ('github.com:22', 'github.com.evil:443', 'evilgithub.com:443', 'github.com.:443', '127.0.0.1:443', 'github.com:0443'):
            with self.subTest(authority=authority), self.assertRaises(ValueError):
                destination(authority, policy, self.answer('140.82.112.3'))
        self.assertEqual(destination('x.actions.githubusercontent.com:443', policy, self.answer('140.82.112.3')), ['140.82.112.3'])

    def test_dns_revalidated_on_every_request(self):
        answers = iter([self.answer('140.82.112.3')(), self.answer('127.0.0.1')()])
        resolver = lambda *args: next(answers)
        destination('github.com:443', ['github.com'], resolver)
        with self.assertRaises(ValueError):
            destination('github.com:443', ['github.com'], resolver)


if __name__ == '__main__':
    unittest.main()
