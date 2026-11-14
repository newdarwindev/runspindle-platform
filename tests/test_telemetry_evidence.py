import copy
import unittest
from tools.collector_privacy_check import CANARY, canary_paths, fixture, outcome


class TelemetryEvidenceTests(unittest.TestCase):
    def test_http_acceptance_without_backend_readback_stays_unknown(self):
        result = outcome(True, [])
        self.assertEqual(result["transport"], "accepted")
        self.assertEqual(result["backend"], "unknown")
        self.assertEqual(result["matching_records"], 0)

    def test_wrong_correlation_does_not_establish_persistence(self):
        for field, replacement in (("traceId", "3" * 32), ("spanId", "4" * 16)):
            record = fixture()
            record["resourceSpans"][0]["scopeSpans"][0]["spans"][0][field] = replacement
            self.assertEqual(outcome(True, [record])["backend"], "unknown")
        record = fixture()
        record["resourceSpans"][0]["resource"]["attributes"][0]["value"][
            "stringValue"
        ] = "other-task"
        self.assertEqual(outcome(True, [record])["backend"], "unknown")

    def test_whole_envelope_including_schema_and_dictionary_keys_is_checked(self):
        record = fixture()
        paths = canary_paths(record)
        self.assertIn("/resourceSpans/0/schemaUrl", paths)
        self.assertIn("/resourceSpans/0/scopeSpans/0/schemaUrl", paths)
        self.assertTrue(any("events" in p for p in paths))
        self.assertEqual(canary_paths({CANARY: "safe"}), ["/key"])
        self.assertEqual(outcome(True, [record])["privacy"], "blocked")

    def test_duplicates_visible_without_inventing_unique_persisted_events(self):
        record = fixture()
        observed = outcome(True, [record, copy.deepcopy(record)])
        self.assertEqual(observed["matching_records"], 2)
        self.assertEqual(observed["duplicates"], 1)
        self.assertEqual(observed["backend"], "persisted_file_only")


if __name__ == "__main__":
    unittest.main()
