"""Actual Manager/SQLite with fake host effects; never starts a guest."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.runner_fleet.model import config
from tools.runner_fleet.telemetry import (
    CAPACITY,
    Buffer,
    ObservedManager,
    Operation,
    Outcome,
)


class TelemetryTests(unittest.TestCase):
    def test_buffer_bounds_and_contention(self) -> None:
        buffer = Buffer()
        for _ in range(CAPACITY + 5):
            buffer.record(Operation.TICK, Outcome.UNKNOWN, 0)
        self.assertEqual(len(buffer.drain()), CAPACITY)
        with buffer._lock:
            buffer.record(Operation.TICK, Outcome.UNKNOWN, 0)
        self.assertEqual(buffer.drain(), ())

    def test_uncertain_cleanup_not_observed_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = Mock()
            backend.directory.return_value = Path(directory)
            backend.destroy.side_effect = RuntimeError("SECRET_CANARY")
            buffer = Buffer()
            manager = ObservedManager(config(), directory, backend, telemetry=buffer)
            self.addCleanup(manager.state.db.close)
            resource = manager.state.reserve(
                {"repository": "runspindle-platform", "id": 44}
            )
            with patch("tools.runner_fleet.manager.helper", return_value={}):
                manager.cleanup(resource, "PRIVATE_REASON")
            self.assertEqual(manager.state.active()[0]["phase"], "uncertain")
            records = buffer.drain()
            self.assertEqual(records[0].outcome, Outcome.ACKNOWLEDGED)
            self.assertNotIn("PRIVATE", repr(records))
            self.assertNotIn("SECRET", repr(records))
            self.assertEqual(
                set(vars(records[0])),
                {"operation", "outcome", "started_ns", "ended_ns"},
            )

    def test_failure_and_cancellation_original_exceptions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            buffer = Buffer()
            manager = ObservedManager(config(), directory, Mock(), telemetry=buffer)
            self.addCleanup(manager.state.db.close)
            for error, outcome in (
                (RuntimeError("SECRET"), Outcome.ERROR),
                (KeyboardInterrupt(), Outcome.UNKNOWN),
            ):
                with patch.object(manager, "inventory", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        manager.tick()
                self.assertIs(raised.exception, error)
                self.assertEqual(buffer.drain()[0].outcome, outcome)

    def test_buffer_failure_does_not_change_default_drain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            buffer = Buffer()
            backend = Mock()
            manager = ObservedManager(config(), directory, backend, telemetry=buffer)
            self.addCleanup(manager.state.db.close)
            with patch.object(Buffer, "record", side_effect=RuntimeError("SECRET")):
                manager.tick(False)
            backend.create.assert_not_called()
            self.assertEqual(manager.state.active(), [])
