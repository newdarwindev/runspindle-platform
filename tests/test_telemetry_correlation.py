"""Offline correlation oracle tests; no Collector or downstream service runs."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import collector_privacy_check as privacy


class CorrelationTests(unittest.TestCase):
    def record(self):
        # Synthetic redaction stand-in, not a qualification of real sanitization.
        return json.loads(json.dumps(privacy.fixture()).replace(privacy.CANARY, "redacted"))

    def resource_attributes(self, record):
        return record["resourceSpans"][0]["resource"]["attributes"]

    def span(self, record):
        return record["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

    def assert_unknown(self, record, checker=privacy.outcome):
        before = copy.deepcopy(record)
        with patch.object(privacy.subprocess, "run", side_effect=AssertionError("forbidden process")) as calls:
            result = checker(True, [record])
        calls.assert_not_called()
        self.assertEqual(result["backend"], "unknown")
        self.assertEqual(result["matching_records"], 0)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(record, before)

    def test_each_context_field_is_required_and_exact(self):
        for key in ["task_id", "session_id", "producer", "config_sha256", "policy_sha256"]:
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

    def test_duplicate_identity_attributes_never_use_last_value(self):
        for scope in ["resource", "span"]:
            for duplicate_first in [False, True]:
                for value in ["wrong-context", "task-17" if scope == "resource" else "event-17"]:
                    record = self.record()
                    attrs = self.resource_attributes(record) if scope == "resource" else self.span(record)["attributes"]
                    duplicate = {"key": "task_id" if scope == "resource" else "event_id", "value": {"stringValue": value}}
                    attrs.insert(0 if duplicate_first else len(attrs), duplicate)
                    self.assert_unknown(record)

    def test_ambiguous_anyvalue_and_malformed_attributes_reject(self):
        for attributes in [None, {}, "attributes", [None], [{"key": "task_id", "value": None}]]:
            record = self.record()
            record["resourceSpans"][0]["resource"]["attributes"] = attributes
            self.assert_unknown(record)
        record = self.record()
        self.resource_attributes(record)[0]["value"]["intValue"] = "17"
        self.assert_unknown(record)
        record = self.record()
        self.span(record)["attributes"][0]["value"]["intValue"] = "17"
        self.assert_unknown(record)

    def test_malformed_hierarchy_does_not_crash_or_match(self):
        for record in [None, [], {}, {"resourceSpans": None}, {"resourceSpans": [None]}, {"resourceSpans": [{"resource": []}]}]:
            self.assert_unknown(record)
        for records in [None, {}, "not-records"]:
            self.assertEqual(privacy.outcome(True, records)["backend"], "unknown")

    def test_only_exact_records_count_and_transport_remains_separate(self):
        valid = self.record()
        wrong = self.record()
        next(a for a in self.resource_attributes(wrong) if a["key"] == "producer")["value"]["stringValue"] = "wrong-producer"
        result = privacy.outcome(False, [wrong, valid, copy.deepcopy(valid)])
        self.assertEqual(result["transport"], "unknown")
        self.assertEqual(result["matching_records"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["backend"], "persisted_file_only")
        for value in ["accepted", 1, [], None]:
            self.assertEqual(privacy.outcome(value, [valid])["transport"], "unknown")

    def test_unmatched_record_still_blocks_whole_envelope_privacy(self):
        wrong = privacy.fixture()
        wrong["resourceSpans"][0]["resource"]["attributes"] = []
        result = privacy.outcome(True, [self.record(), wrong])
        self.assertEqual(result["matching_records"], 1)
        self.assertEqual(result["privacy"], "blocked")

    def test_context_guard_mutation_fails_independent_oracle(self):
        source = Path(privacy.__file__).read_text()
        anchor = 'any(attrs.get(key) != value for key, value in expected_resource.items())'
        self.assertEqual(source.count(anchor), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mutant.py"
            path.write_text(source.replace(anchor, "False"))
            spec = importlib.util.spec_from_file_location("correlation_mutant", path)
            mutant = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mutant)
            record = self.record()
            next(a for a in self.resource_attributes(record) if a["key"] == "policy_sha256")["value"]["stringValue"] = "c" * 64
            with self.assertRaisesRegex(AssertionError, "persisted_file_only.*unknown"):
                self.assert_unknown(record, mutant.outcome)


if __name__ == "__main__":
    unittest.main()
