"""Offline evidence snapshots, not a new receiver or runtime telemetry harness."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.check_native_monitoring import ARTIFACTS, ROOT, validate
from tools.collector_privacy_check import CANARY, canary_paths, fixture, outcome


def trusted_fixture():
    # Only construct a non-secret fixture. This is NOT a telemetry sanitizer.
    return json.loads(json.dumps(fixture()).replace(CANARY, "synthetic-safe-value"))


class NativeMonitoringEvidenceTests(unittest.TestCase):
    def test_whole_envelope_canary_fixture_is_not_qualified(self):
        record = fixture()
        self.assertGreater(len(canary_paths(record)), 8)
        self.assertEqual(outcome(True, [record])["privacy"], "blocked")
        # Persistence may be observed even when privacy fails.
        self.assertEqual(outcome(True, [record])["backend"], "persisted_file_only")

    def test_offline_flush_snapshots_do_not_certify_client_drain(self):
        self.assertEqual(outcome(True, [])["backend"], "unknown")
        after = outcome(True, [trusted_fixture()])
        self.assertEqual(after["backend"], "persisted_file_only")
        self.assertEqual(after["matching_records"], 1)
        self.assertNotIn("native_flush", after)

    def test_offline_outage_snapshots_do_not_invent_durable_delivery(self):
        before = outcome(True, [])
        self.assertEqual(before["transport"], "accepted")
        self.assertEqual(before["backend"], "unknown")
        self.assertEqual(outcome(False, [])["backend"], "unknown")
        after = outcome(True, [trusted_fixture(), trusted_fixture()])
        self.assertEqual(after["duplicates"], 1)
        self.assertEqual(after["backend"], "persisted_file_only")

    def test_readback_and_transport_are_independent(self):
        result = outcome(False, [trusted_fixture()])
        self.assertEqual(result["transport"], "unknown")
        self.assertEqual(result["backend"], "persisted_file_only")

    def test_event_or_session_mismatch_cannot_establish_readback(self):
        for kind in ("event", "session"):
            record = trusted_fixture()
            resource = record["resourceSpans"][0]
            if kind == "event":
                attrs = resource["scopeSpans"][0]["spans"][0]["attributes"]
                key = "event_id"
            else:
                attrs, key = resource["resource"]["attributes"], "session_id"
            next(a for a in attrs if a["key"] == key)["value"]["stringValue"] = "wrong"
            self.assertEqual(outcome(True, [record])["backend"], "unknown")

    def test_allowed_identity_canary_remains_a_privacy_blocker(self):
        record = trusted_fixture()
        resource = record["resourceSpans"][0]["resource"]["attributes"]
        next(a for a in resource if a["key"] == "producer")["value"]["stringValue"] = CANARY
        self.assertEqual(outcome(True, [record])["privacy"], "blocked")


class NativeMonitoringDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ARTIFACTS | {"telemetry/native-monitoring-lift.json"}:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)

    def edit(self, name, change):
        path = self.root / name
        obj = json.loads(path.read_text())
        change(obj)
        path.write_text(json.dumps(obj))
        if name != "telemetry/native-monitoring-lift.json":
            manifest = self.root / "telemetry/native-monitoring-lift.json"
            data = json.loads(manifest.read_text())
            data["artifact_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(data))

    def test_checked_in_artifacts_pass(self):
        self.assertEqual(validate(self.root)["source_units"], 19)

    def test_artifact_tamper_rejected(self):
        with (self.root / "specs/native-monitoring.md").open("a") as out:
            out.write("changed")
        with self.assertRaisesRegex(ValueError, "artifact drift"):
            validate(self.root)

    def test_incomplete_source_accounting_rejected(self):
        self.edit("telemetry/native-monitoring-lift.json", lambda d: d["source_units"].pop())
        with self.assertRaisesRegex(ValueError, "source accounting"):
            validate(self.root)

    def test_missing_destination_rejected(self):
        self.edit("telemetry/native-monitoring-lift.json", lambda d:
                  d["source_units"][0].update(destination="specs/native-monitoring.md#missing"))
        with self.assertRaisesRegex(ValueError, "destination"):
            validate(self.root)

    def test_installed_support_promotion_rejected_even_with_rehashed_matrix(self):
        self.edit("telemetry/native-monitoring-compatibility.json", lambda d:
                  d["surfaces"][0].update(installed_qualified=True))
        with self.assertRaisesRegex(ValueError, "runtime claim"):
            validate(self.root)

    def test_floating_source_reference_rejected(self):
        self.edit("telemetry/native-monitoring-compatibility.json", lambda d:
                  d["public_sources"][0].update(revision="main"))
        with self.assertRaisesRegex(ValueError, "unpinned"):
            validate(self.root)

    def test_unknown_surface_cannot_disappear(self):
        self.edit("telemetry/native-monitoring-compatibility.json", lambda d:
                  d["surfaces"].pop(3))
        with self.assertRaisesRegex(ValueError, "surface coverage"):
            validate(self.root)

    def test_original_expression_cannot_be_marked_imported(self):
        self.edit("telemetry/native-monitoring-lift.json", lambda d:
                  d.update(original_expression="imported"))
        with self.assertRaisesRegex(ValueError, "unsupported claim"):
            validate(self.root)

    def test_manifest_path_inventory_cannot_escape(self):
        self.edit("telemetry/native-monitoring-lift.json", lambda d:
                  d["artifact_sha256"].update({"../outside": "a" * 64}))
        with self.assertRaisesRegex(ValueError, "inventory"):
            validate(self.root)


if __name__ == "__main__":
    unittest.main()
