#!/usr/bin/env python3
"""Offline SRC-011 artifact/evidence checks; never start a client or service."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PIN = "c4d748f586a84a3ed5b6aceb82e9a1db4abb1cda"
SURFACES = {
    "cli": "source_review_only",
    "app_server": "source_review_only",
    "ide_desktop": "unknown",
    "hosted": "unknown",
    "collector_traces": "prior_synthetic_file_lane_only",
    "native_logs": "unqualified",
    "bounded_messages": "unavailable",
    "langfuse": "blocked",
    "tokscale": "unqualified",
    "homarr": "unqualified",
    "agentsview": "unavailable",
    "langfuse_plugin": "excluded",
    "full_conversations": "unavailable",
    "optional_backends": "unqualified",
}
ARTIFACTS = {
    "specs/native-monitoring.md",
    "telemetry/native-monitoring-compatibility.json",
    "tools/check_native_monitoring.py",
    "tools/validate.py",
    "tests/test_native_monitoring.py",
    "tests/test_telemetry_evidence.py",
    "tools/collector_privacy_check.py",
    "tools/collector_recovery_check.py",
    "telemetry/collector.yaml",
    "telemetry/dependencies.json",
    "telemetry/langfuse-qualification.json",
    "docs/maintained-offline-tests.json",
    "docs/native-monitoring-validation.json",
    "provenance.json",
    "STATUS.json",
}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def read(root, name):
    return json.loads((root / name).read_text())


def validate(root=ROOT):
    manifest = read(root, "telemetry/native-monitoring-lift.json")
    require(manifest["schema_version"] == 1, "unknown lift schema")
    require(manifest["source_id"] == "SRC-011", "source identity changed")
    for field, expected in {
        "classification": "original_concept_document_lift",
        "original_expression": "excluded_missing_rights_clearance",
        "private_dependencies": "excluded_unreviewed",
        "historical_reference": "exact_identity_retained_privately",
        "adapted_content": "all_selected_source_sections_accounted_with_exclusions",
        "executable_implementation": "original_offline_checker_and_tests_only",
        "runtime_validation": "prior_synthetic_evidence_referenced_not_rerun",
        "native_telemetry": "blocked",
        "live_acceptance": "not_run",
    }.items():
        require(manifest[field] == expected, "unsupported claim: " + field)
    require(manifest["independent_review"] in {"pending", "passed"}, "review state")
    hashes = manifest["artifact_sha256"]
    require(set(hashes) == ARTIFACTS, "incomplete or unexpected artifact inventory")
    for name, expected in hashes.items():
        path = root / name
        require(path.is_file() and not path.is_symlink(), "missing/linked artifact")
        require(path.resolve().is_relative_to(root.resolve()), "artifact path escaped")
        require(re.fullmatch(r"[0-9a-f]{64}", expected), "invalid digest")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                "artifact drift: " + name)
    document = (root / "specs/native-monitoring.md").read_text()
    anchors = {line[3:].lower().replace(" ", "-")
               for line in document.splitlines() if line.startswith("## ")}
    units = manifest["source_units"]
    require([unit["id"] for unit in units] ==
            [f"SRC-011-{i:02}" for i in range(1, 20)], "source accounting incomplete")
    for unit in units:
        file, anchor = unit["destination"].split("#")
        require(file == "specs/native-monitoring.md" and anchor in anchors,
                "missing section destination")
        require(unit["disposition"] ==
                "original_concept_adaptation_with_original_expression_and_private_dependency_exclusions",
                "rights exclusion removed")
    matrix = read(root, "telemetry/native-monitoring-compatibility.json")
    require(matrix["schema_version"] == 1 and matrix["source_id"] == "SRC-011",
            "unknown compatibility schema")
    require(matrix["source_bytes_imported"] is False, "upstream import not cleared")
    rows = matrix["surfaces"]
    require(len(rows) == len(SURFACES) and
            {r["surface"]: r["status"] for r in rows} == SURFACES,
            "surface coverage/support changed")
    sources = matrix["public_sources"]
    expected_paths = {
        "schema": "codex-rs/core/config.schema.json",
        "version": "codex-rs/Cargo.toml",
        "provider": "codex-rs/otel/src/provider.rs",
        "initialization": "codex-rs/core/src/otel_init.rs",
        "cli": "codex-rs/exec/src/lib.rs",
        "app_server": "codex-rs/app-server/src/lib.rs",
        "events": "codex-rs/otel/src/events/session_telemetry.rs",
    }
    require(len(sources) == len(expected_paths) and
            {s["id"]: s["path"] for s in sources} == expected_paths,
            "source citation inventory changed")
    for source in sources:
        require(source["repository"] == "openai/codex" and
                source["revision"] == PIN and
                re.fullmatch(r"[0-9a-f]{40}", source["git_blob_sha"]) and
                source["url"] == f"https://github.com/openai/codex/blob/{PIN}/{source['path']}",
                "unpinned or unexpected public citation")
    require(set(matrix["configuration_keys"]) == {
        "otel." + key for key in ("environment", "exporter", "log_user_prompt",
        "metrics_exporter", "span_attributes", "trace_exporter", "tracestate")
    } and matrix["http_protocols"] == ["binary", "json"], "unreviewed settings")
    for row in rows:
        require(row["installed_qualified"] is False and
                row["live_acceptance"] == "not_run", "unverified runtime claim")
        if row["surface"] in {"cli", "app_server"}:
            require(row["version"] == "0.143.0" and row["source_revision"] == PIN
                    and set(row["source_evidence"]) ==
                    {"schema", row["surface"], "provider", "version"},
                    "client pin/citations missing")
        elif row["surface"] in {"collector_traces", "langfuse"}:
            file = "telemetry/" + ("dependencies.json" if row["surface"] ==
                                   "collector_traces" else "langfuse-qualification.json")
            dep = read(root, file)
            require(row["source_revision"] == dep["source_revision"] and
                    row["version"] == dep["version"], "dependency pin drift")
        else:
            require(row["source_revision"] is None and row["version"] is None,
                    "unreviewed surface pin")
    prior_path = manifest["prior_evidence"]
    require(prior_path == "docs/maintained-offline-tests.json", "wrong prior evidence")
    prior = read(root, prior_path)
    for name in ("tools/collector_privacy_check.py", "tools/collector_recovery_check.py",
                 "tests/test_telemetry_evidence.py", "telemetry/collector.yaml"):
        require(prior["source_hashes"][name] == hashes[name], "prior source evidence drift")
    for key in ("Collector_privacy", "Collector_queue_recovery"):
        require(prior[key]["observed"]["backend"] == "persisted_file_only",
                "prior evidence scope changed")
    require(prior["live_acceptance"] == "not_run", "prior live claim changed")
    source = next(s for s in read(root, "provenance.json")["sources"]
                  if s["source_id"] == "SRC-011")
    require(source["artifact_manifest"] == "telemetry/native-monitoring-lift.json"
            and source["live_acceptance"] == "not_run", "provenance mismatch")
    status = read(root, "STATUS.json")["native_monitoring_document_lift"]
    require(status["native_telemetry"] == "blocked" and
            status["live_acceptance"] == "not_run" and
            status["independent_review"] == manifest["independent_review"],
            "status mismatch")
    return {"status": "passed", "evidence_class": "offline_document_contract",
            "source_units": len(units), "surfaces": len(rows),
            "runtime_execution": "not_run", "live_acceptance": "not_run"}


if __name__ == "__main__":
    print(json.dumps(validate(), sort_keys=True))
