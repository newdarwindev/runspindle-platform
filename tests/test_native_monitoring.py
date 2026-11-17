"""Offline evidence snapshots, not a new receiver or runtime telemetry harness."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from tools import collector_privacy_check as legacy
from tools.check_native_monitoring import (
    ARTIFACTS,
    ROOT,
    JsonObject,
    JsonValue,
    array,
    object_value,
    read,
    validate,
)

# Precise callable boundary for the current, hash-pinned synthetic fixture helpers.
# These signatures add no runtime behavior, protocol widening or type suppression.
fixture: Callable[[], JsonObject] = legacy.fixture
canary_paths: Callable[[JsonValue], list[str]] = legacy.canary_paths
outcome: Callable[[bool, list[JsonObject]], JsonObject] = legacy.outcome
CANARY = legacy.CANARY


def trusted_fixture() -> JsonObject:
    # Construct a fixture, not a sanitizer: edit the existing synthetic canary strings.
    def replace(value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return value.replace(CANARY, "synthetic-safe-value")
        if isinstance(value, dict):
            return {
                k.replace(CANARY, "synthetic-safe-value"): replace(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [replace(v) for v in value]
        return value

    return object_value(replace(fixture()))


def resource(record: JsonObject) -> JsonObject:
    return object_value(array(record["resourceSpans"])[0])


def attribute(attributes: JsonValue, key: str) -> JsonObject:
    for value in array(attributes):
        attr = object_value(value)
        if attr["key"] == key:
            return object_value(attr["value"])
    raise ValueError("fixture attribute missing")


class NativeMonitoringEvidenceTests(unittest.TestCase):
    def test_whole_envelope_canary_fixture_is_not_qualified(self) -> None:
        record = fixture()
        self.assertGreater(len(canary_paths(record)), 8)
        self.assertEqual(outcome(True, [record])["privacy"], "blocked")
        self.assertEqual(outcome(True, [record])["backend"], "persisted_file_only")

    def test_offline_flush_snapshots_do_not_certify_client_drain(self) -> None:
        self.assertEqual(outcome(True, [])["backend"], "unknown")
        after = outcome(True, [trusted_fixture()])
        self.assertEqual(after["backend"], "persisted_file_only")
        self.assertEqual(after["matching_records"], 1)
        self.assertNotIn("native_flush", after)

    def test_offline_outage_snapshots_do_not_invent_durable_delivery(self) -> None:
        before = outcome(True, [])
        self.assertEqual(before["transport"], "accepted")
        self.assertEqual(before["backend"], "unknown")
        self.assertEqual(outcome(False, [])["backend"], "unknown")
        after = outcome(True, [trusted_fixture(), trusted_fixture()])
        self.assertEqual(after["duplicates"], 1)
        self.assertEqual(after["backend"], "persisted_file_only")

    def test_readback_and_transport_are_independent(self) -> None:
        result = outcome(False, [trusted_fixture()])
        self.assertEqual(result["transport"], "unknown")
        self.assertEqual(result["backend"], "persisted_file_only")

    def test_event_or_session_mismatch_cannot_establish_readback(self) -> None:
        for kind in ("event", "session"):
            record = trusted_fixture()
            res = resource(record)
            if kind == "event":
                scope = object_value(array(res["scopeSpans"])[0])
                span = object_value(array(scope["spans"])[0])
                attribute(span["attributes"], "event_id")["stringValue"] = "wrong"
            else:
                attribute(object_value(res["resource"])["attributes"], "session_id")[
                    "stringValue"
                ] = "wrong"
            self.assertEqual(outcome(True, [record])["backend"], "unknown")

    def test_allowed_identity_canary_remains_a_privacy_blocker(self) -> None:
        record = trusted_fixture()
        attrs = object_value(resource(record)["resource"])["attributes"]
        attribute(attrs, "producer")["stringValue"] = CANARY
        self.assertEqual(outcome(True, [record])["privacy"], "blocked")


class NativeMonitoringDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ARTIFACTS | {"telemetry/native-monitoring-lift.json"}:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)

    def edit(
        self,
        name: str,
        keys: list[str | int],
        replacement: JsonValue = None,
        *,
        delete: bool = False,
    ) -> None:
        path = self.root / name
        obj = read(self.root, name)
        current: JsonValue = obj
        for key in keys[:-1]:
            current = (
                array(current)[key]
                if isinstance(key, int)
                else object_value(current)[key]
            )
        final = keys[-1]
        if isinstance(final, int):
            if delete:
                del array(current)[final]
            else:
                array(current)[final] = replacement
        elif delete:
            del object_value(current)[final]
        else:
            object_value(current)[final] = replacement
        path.write_text(json.dumps(obj))
        if name != "telemetry/native-monitoring-lift.json":
            manifest_path = self.root / "telemetry/native-monitoring-lift.json"
            manifest = read(self.root, "telemetry/native-monitoring-lift.json")
            object_value(manifest["artifact_sha256"])[name] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest))

    def assert_invalid(self, reason: str) -> None:
        with self.assertRaisesRegex(ValueError, reason):
            validate(self.root)

    def test_checked_in_artifacts_pass(self) -> None:
        self.assertEqual(validate(self.root)["source_units"], 19)

    def test_artifact_tamper_rejected(self) -> None:
        with (self.root / "specs/native-monitoring.md").open("a") as out:
            out.write("changed")
        self.assert_invalid("artifact drift")

    def test_current_harness_and_status_drift_rejected(self) -> None:
        for name in ("STATUS.json", "tools/collector_privacy_check.py"):
            with self.subTest(artifact=name):
                path = self.root / name
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\n")
                    self.assert_invalid("artifact drift: " + name)
                finally:
                    path.write_bytes(original)

    def test_rehashed_unknown_harness_rejected(self) -> None:
        name = "tools/collector_privacy_check.py"
        path = self.root / name
        path.write_bytes(path.read_bytes() + b"\n")
        self.edit(
            "telemetry/native-monitoring-lift.json",
            ["artifact_sha256", name],
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        self.assert_invalid("prior source evidence drift")

    def test_historical_source_identity_cannot_be_rewritten(self) -> None:
        self.edit(
            "docs/maintained-offline-tests.json",
            ["source_hashes", "tools/collector_privacy_check.py"],
            "4ccd22cb3bd80727cafd7256963480a6aaa806f3d8dfddeca57e2e76c999efa5",
        )
        self.assert_invalid("prior source evidence drift")

    def test_current_runtime_remains_unqualified(self) -> None:
        result = validate(self.root)
        self.assertEqual(result["current_harness_runtime_qualification"], "not_run")
        self.assertEqual(
            result["prior_runtime_evidence"], "historical_only_classifier_changed"
        )

    def test_incomplete_source_accounting_rejected(self) -> None:
        self.edit(
            "telemetry/native-monitoring-lift.json", ["source_units", -1], delete=True
        )
        self.assert_invalid("source accounting")

    def test_missing_destination_rejected(self) -> None:
        self.edit(
            "telemetry/native-monitoring-lift.json",
            ["source_units", 0, "destination"],
            "specs/native-monitoring.md#missing",
        )
        self.assert_invalid("destination")

    def test_installed_support_promotion_rejected_even_with_rehashed_matrix(
        self,
    ) -> None:
        self.edit(
            "telemetry/native-monitoring-compatibility.json",
            ["surfaces", 0, "installed_qualified"],
            True,
        )
        self.assert_invalid("runtime claim")

    def test_floating_source_reference_rejected(self) -> None:
        self.edit(
            "telemetry/native-monitoring-compatibility.json",
            ["public_sources", 0, "revision"],
            "main",
        )
        self.assert_invalid("unpinned")

    def test_unknown_surface_cannot_disappear(self) -> None:
        self.edit(
            "telemetry/native-monitoring-compatibility.json",
            ["surfaces", 3],
            delete=True,
        )
        self.assert_invalid("surface coverage")

    def test_original_expression_cannot_be_marked_imported(self) -> None:
        self.edit(
            "telemetry/native-monitoring-lift.json", ["original_expression"], "imported"
        )
        self.assert_invalid("unsupported claim")

    def test_manifest_path_inventory_cannot_escape(self) -> None:
        self.edit(
            "telemetry/native-monitoring-lift.json",
            ["artifact_sha256", "../outside"],
            "a" * 64,
        )
        self.assert_invalid("inventory")

    def test_status_rights_cannot_conflict_with_manifest(self) -> None:
        self.edit(
            "STATUS.json",
            ["native_monitoring_document_lift", "original_expression"],
            "imported",
        )
        self.assert_invalid("status: original_expression")

    def test_status_reference_cannot_target_another_manifest(self) -> None:
        self.edit(
            "STATUS.json",
            ["native_monitoring_document_lift", "manifest"],
            "telemetry/missing.json",
        )
        self.assert_invalid("status: manifest")

    def test_status_source_accounting_cannot_change(self) -> None:
        self.edit(
            "STATUS.json",
            ["native_monitoring_document_lift", "source_accounting"],
            "original_import_complete",
        )
        self.assert_invalid("status: source_accounting")

    def source_index(self) -> int:
        sources = array(read(self.root, "provenance.json")["sources"])
        return next(
            i
            for i, s in enumerate(sources)
            if object_value(s)["source_id"] == "SRC-011"
        )

    def test_provenance_runtime_cannot_claim_native_success(self) -> None:
        self.edit(
            "provenance.json",
            ["sources", self.source_index(), "runtime_validation"],
            "native_client_and_backend_passed",
        )
        self.assert_invalid("provenance: runtime_validation")

    def test_provenance_reference_cannot_target_another_matrix(self) -> None:
        self.edit(
            "provenance.json",
            ["sources", self.source_index(), "compatibility_matrix"],
            "telemetry/missing.json",
        )
        self.assert_invalid("provenance: compatibility_matrix")

    def test_receipt_cannot_claim_native_evidence(self) -> None:
        self.edit(
            "docs/native-monitoring-validation.json",
            ["evidence_class"],
            "native_live_acceptance",
        )
        self.assert_invalid("validation receipt: evidence_class")

    def test_receipt_cannot_erase_unrun_scope(self) -> None:
        self.edit("docs/native-monitoring-validation.json", ["not_run"], [])
        self.assert_invalid("validation receipt: not_run")

    def test_receipt_cannot_fabricate_native_command(self) -> None:
        self.edit(
            "docs/native-monitoring-validation.json",
            ["commands", 0, "command"],
            "native client and backend check",
        )
        self.assert_invalid("unreviewed validation command")

    def test_boolean_schema_versions_rejected(self) -> None:
        for name in (
            "telemetry/native-monitoring-lift.json",
            "telemetry/native-monitoring-compatibility.json",
            "docs/native-monitoring-validation.json",
        ):
            with self.subTest(name=name):
                self.edit(name, ["schema_version"], True)
                self.assert_invalid("schema_version")
                self.edit(name, ["schema_version"], 1)

    def test_unreviewed_extra_receipt_claim_is_rejected(self) -> None:
        self.edit(
            "docs/native-monitoring-validation.json", ["native_telemetry"], "passed"
        )
        self.assert_invalid("unreviewed validation receipt field")


if __name__ == "__main__":
    unittest.main()
