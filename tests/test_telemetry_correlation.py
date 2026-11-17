"""Offline correlation oracle tests; no Collector or downstream service runs."""

from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from tools import collector_privacy_check as privacy

# Typed boundary to existing untyped synthetic JSON fixture helpers; arbitrary
# malformed input is intentional. This does not suppress checking these tests.
fixture: Callable[[], dict[str, Any]] = privacy.fixture
outcome: Callable[[object, object], dict[str, Any]] = privacy.outcome


class CorrelationTests(unittest.TestCase):
    def record(self) -> dict[str, Any]:
        # Synthetic redaction stand-in, not a qualification of real sanitization.
        return cast(
            dict[str, Any],
            json.loads(json.dumps(fixture()).replace(privacy.CANARY, "redacted")),
        )

    def resource_attributes(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]], record["resourceSpans"][0]["resource"]["attributes"]
        )

    def span(self, record: dict[str, Any]) -> dict[str, Any]:
        return cast(
            dict[str, Any], record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )

    def assert_unknown(
        self,
        record: object,
        checker: Callable[[object, object], dict[str, Any]] = outcome,
    ) -> None:
        before = copy.deepcopy(record)
        with patch(
            "tools.collector_privacy_check.subprocess.run",
            side_effect=AssertionError("forbidden process"),
        ) as calls:
            result = checker(True, [record])
        calls.assert_not_called()
        self.assertEqual(result["backend"], "unknown")
        self.assertEqual(result["matching_records"], 0)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(record, before)

    def test_each_context_field_is_required_and_exact(self) -> None:
        for key in [
            "task_id",
            "session_id",
            "producer",
            "config_sha256",
            "policy_sha256",
        ]:
            for replacement in [None, "wrong-context", 1, True]:
                record = self.record()
                attrs = self.resource_attributes(record)
                match = next(a for a in attrs if a["key"] == key)
                if replacement is None:
                    attrs.remove(match)
                else:
                    match["value"]["stringValue"] = replacement
                with self.subTest(key=key, replacement=replacement):
                    self.assert_unknown(record)

    def test_duplicate_identity_attributes_never_use_last_value(self) -> None:
        for scope in ["resource", "span"]:
            for duplicate_first in [False, True]:
                for value in [
                    "wrong-context",
                    "task-17" if scope == "resource" else "event-17",
                ]:
                    record = self.record()
                    attrs = (
                        self.resource_attributes(record)
                        if scope == "resource"
                        else self.span(record)["attributes"]
                    )
                    duplicate = {
                        "key": "task_id" if scope == "resource" else "event_id",
                        "value": {"stringValue": value},
                    }
                    attrs.insert(0 if duplicate_first else len(attrs), duplicate)
                    self.assert_unknown(record)

    def test_ambiguous_anyvalue_and_malformed_attributes_reject(self) -> None:
        malformed: list[object] = [
            None,
            {},
            "attributes",
            [None],
            [{"key": "task_id", "value": None}],
        ]
        for attributes in malformed:
            record = self.record()
            record["resourceSpans"][0]["resource"]["attributes"] = attributes
            self.assert_unknown(record)
        record = self.record()
        self.resource_attributes(record)[0]["value"]["intValue"] = "17"
        self.assert_unknown(record)
        record = self.record()
        self.span(record)["attributes"][0]["value"]["intValue"] = "17"
        self.assert_unknown(record)

    def test_malformed_hierarchy_does_not_crash_or_match(self) -> None:
        malformed: list[object] = [
            None,
            [],
            {},
            {"resourceSpans": None},
            {"resourceSpans": [None]},
            {"resourceSpans": [{"resource": []}]},
        ]
        for record in malformed:
            self.assert_unknown(record)
        malformed_records: list[object] = [None, {}, "not-records"]
        for records in malformed_records:
            self.assertEqual(outcome(True, records)["backend"], "unknown")

    def test_only_exact_records_count_and_transport_remains_separate(self) -> None:
        valid = self.record()
        wrong = self.record()
        next(a for a in self.resource_attributes(wrong) if a["key"] == "producer")[
            "value"
        ]["stringValue"] = "wrong-producer"
        result = outcome(False, [wrong, valid, copy.deepcopy(valid)])
        self.assertEqual(result["transport"], "unknown")
        self.assertEqual(result["matching_records"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["backend"], "persisted_file_only")
        for value in ["accepted", 1, [], None]:
            self.assertEqual(outcome(value, [valid])["transport"], "unknown")

    def test_unmatched_record_still_blocks_whole_envelope_privacy(self) -> None:
        wrong = fixture()
        wrong["resourceSpans"][0]["resource"]["attributes"] = []
        result = outcome(True, [self.record(), wrong])
        self.assertEqual(result["matching_records"], 1)
        self.assertEqual(result["privacy"], "blocked")

    def test_context_guard_mutation_fails_independent_oracle(self) -> None:
        if privacy.__file__ is None:
            self.fail("privacy module source missing")
        source = Path(privacy.__file__).read_text()
        anchor = (
            "any(attrs.get(key) != value for key, value in expected_resource.items())"
        )
        self.assertEqual(source.count(anchor), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mutant.py"
            path.write_text(source.replace(anchor, "False"))
            spec = importlib.util.spec_from_file_location("correlation_mutant", path)
            if spec is None or spec.loader is None:
                self.fail("mutation module loader missing")
            mutant = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mutant)
            record = self.record()
            next(
                a
                for a in self.resource_attributes(record)
                if a["key"] == "policy_sha256"
            )["value"]["stringValue"] = "c" * 64
            with self.assertRaisesRegex(AssertionError, "persisted_file_only.*unknown"):
                self.assert_unknown(record, mutant.outcome)


if __name__ == "__main__":
    unittest.main()
