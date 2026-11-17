"""Real synthetic SQLite state: reject damage before capacity can be reused."""

from __future__ import annotations

import importlib.util
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
module: Any = importlib.import_module("runner_fleet.state")
SCHEMA_STATEMENTS = module.SCHEMA_STATEMENTS
SchemaRejected = module.SchemaRejected
State = module.State

ROOT = Path(__file__).resolve().parents[1]


class FleetSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        deny = patch.object(socket, "socket", side_effect=AssertionError("network"))
        spy = deny.start()
        self.addCleanup(deny.stop)
        self.addCleanup(spy.assert_not_called)

    def seed(self) -> None:
        state = State(self.root)
        first = state.reserve({"repository": "synthetic", "id": 1})
        state.update(first, "uncertain")
        state.reserve({"repository": "synthetic", "id": 2})
        state.db.close()

    def test_exact_existing_layout_preserves_unknown_occupancy(self) -> None:
        self.seed()
        before = (self.root / "fleet.sqlite").read_bytes()
        state = State(self.root)
        try:
            self.assertEqual(len(state.active()), 2)
            with self.assertRaises(RuntimeError):
                state.reserve({"repository": "synthetic", "id": 3})
        finally:
            state.db.close()
        self.assertEqual((self.root / "fleet.sqlite").read_bytes(), before)

    def test_damaged_layouts_rejected_without_repair(self) -> None:
        for statement in (
            "DROP TABLE resources",
            "DROP TABLE control",
            "DROP TABLE events",
            "DROP TABLE mac_job_observations",
            "DROP INDEX occupied_slot",
            "DROP INDEX occupied_job",
            "ALTER TABLE resources ADD COLUMN unknown TEXT",
            "CREATE TRIGGER extra AFTER INSERT ON resources BEGIN DELETE FROM resources; END",
            "PRAGMA user_version=99",
        ):
            with (
                self.subTest(statement=statement),
                tempfile.TemporaryDirectory() as tmp,
            ):
                state = State(tmp)
                state.reserve({"repository": "synthetic", "id": 1})
                state.db.execute(statement)
                state.db.commit()
                state.db.close()
                path = Path(tmp) / "fleet.sqlite"
                before = path.read_bytes()
                with self.assertRaises(SchemaRejected):
                    State(tmp)
                self.assertEqual(path.read_bytes(), before)

    def test_existing_empty_or_partial_store_is_not_initialized(self) -> None:
        path = self.root / "fleet.sqlite"
        path.touch()
        with self.assertRaises(SchemaRejected):
            State(self.root)
        self.assertEqual(path.read_bytes(), b"")
        with sqlite3.connect(path) as db:
            db.execute(SCHEMA_STATEMENTS[0])
        before = path.read_bytes()
        with self.assertRaises(SchemaRejected):
            State(self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_schema_damage_after_open_blocks_reads_and_allocation(self) -> None:
        state = State(self.root)
        try:
            state.db.execute("DROP INDEX occupied_slot")
            state.db.commit()
            before = tuple(state.db.iterdump())
            for operation in (
                state.active,
                lambda: state.mac_job_observation("synthetic", 1),
                lambda: state.get("drained"),
                lambda: state.set("drained", False),
                lambda: state.reserve({"repository": "synthetic", "id": 1}),
            ):
                with self.assertRaises(SchemaRejected):
                    operation()
                self.assertEqual(tuple(state.db.iterdump()), before)
        finally:
            state.db.close()

    def test_schema_mutation_inside_transaction_rolls_back(self) -> None:
        state = State(self.root)
        try:
            before = tuple(state.db.iterdump())
            with self.assertRaises(SchemaRejected), state.transaction():
                state.db.execute("DROP INDEX occupied_slot")
            self.assertEqual(tuple(state.db.iterdump()), before)
        finally:
            state.db.close()

    def test_temp_schema_is_not_accepted(self) -> None:
        state = State(self.root)
        try:
            state.db.execute("CREATE TEMP TABLE resources(dummy TEXT)")
            with self.assertRaises(SchemaRejected):
                state.reserve({"repository": "synthetic", "id": 1})
        finally:
            state.db.close()

    def test_fresh_process_cannot_recreate_lost_reservations(self) -> None:
        self.seed()
        path = self.root / "fleet.sqlite"
        with sqlite3.connect(path) as db:
            db.execute("DROP TABLE resources")
        before = path.read_bytes()
        script = """
import sys
sys.path.insert(0, 'tools')
from runner_fleet.state import State, SchemaRejected
try: State(sys.argv[1])
except SchemaRejected: print('blocked-no-allocation')
else: raise AssertionError('damaged capacity accepted')
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "blocked-no-allocation")
        self.assertEqual(path.read_bytes(), before)

    def test_removed_schema_guard_admits_missing_slot_exclusion(self) -> None:
        self.seed()
        with sqlite3.connect(self.root / "fleet.sqlite") as db:
            db.execute("DROP INDEX occupied_slot")
        source = (ROOT / "tools/runner_fleet/state.py").read_text()
        # Removing validation admits damage that can no longer enforce slot
        # exclusivity. This mutation isolates the admission guard itself.
        anchor = "            _validate_schema(self.db)\n            self.db.commit()"
        self.assertEqual(source.count(anchor), 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mutant.py"
            path.write_text(source.replace(anchor, "            self.db.commit()", 1))
            spec = importlib.util.spec_from_file_location("mutant_state", path)
            if spec is None or spec.loader is None:
                self.fail("missing mutation loader")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            state = module.State(self.root)
            try:
                indexes = {
                    row[0]
                    for row in state.db.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertNotIn("occupied_slot", indexes)
                self.assertEqual(
                    state.db.execute("SELECT count(*) FROM resources").fetchone()[0], 2
                )
            finally:
                state.db.close()
