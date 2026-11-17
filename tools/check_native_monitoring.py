#!/usr/bin/env python3
"""Offline SRC-011 artifact/evidence checks; never start a client or service."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TypeAlias, cast

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]
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
MANIFEST_FIELDS: JsonObject = {
    "schema_version": 1,
    "source_id": "SRC-011",
    "classification": "original_concept_document_lift",
    "original_expression": "excluded_missing_rights_clearance",
    "private_dependencies": "excluded_unreviewed",
    "historical_reference": "exact_identity_retained_privately",
    "adapted_content": "all_selected_source_sections_accounted_with_exclusions",
    "executable_implementation": "original_offline_checker_and_tests_only",
    "runtime_validation": "prior_synthetic_evidence_referenced_not_rerun",
    "native_telemetry": "blocked",
    "live_acceptance": "not_run",
    "independent_review": "pending",
    "prior_evidence": "docs/maintained-offline-tests.json",
}
STATUS_FIELDS: JsonObject = {
    "manifest": "telemetry/native-monitoring-lift.json",
    "source_accounting": "complete_with_explicit_exclusions",
    "original_expression": "excluded_missing_rights_clearance",
    "independent_review": "pending",
    "native_telemetry": "blocked",
    "live_acceptance": "not_run",
}
PROVENANCE_FIELDS: JsonObject = {
    "source_id": "SRC-011",
    "status": "adapted_spec",
    "destination": "specs/native-monitoring.md",
    "modifications": "Original concept rewrite accounting for all selected source sections; original expression and private linked dependencies excluded. No executable legacy import.",
    "license_review": "No blanket source license established; original expression excluded. Public upstream sources referenced, not redistributed.",
    "artifact_manifest": "telemetry/native-monitoring-lift.json",
    "compatibility_matrix": "telemetry/native-monitoring-compatibility.json",
    "runtime_validation": "prior_synthetic_evidence_referenced_not_rerun",
    "live_acceptance": "not_run",
}
NOT_RUN = [
    "native_client",
    "Collector_runtime",
    "Langfuse",
    "TLS_auth_denial",
    "native_shutdown_flush",
    "backend_API_readback",
    "authenticated_UI",
    "hosted_telemetry",
    "deployment",
    "installation",
    "enrollment",
    "GitHub_CI",
    "independent_review",
]
RECEIPT_FIELDS: JsonObject = {
    "schema_version": 1,
    "source_id": "SRC-011",
    "base_revision": "0df57c2283d973f695b017ce35855b14d160516c",
    "evidence_class": "offline_document_and_evidence_contract_tests",
    "synthetic_lifecycle_tests": "snapshot classification only; not actual flushing, outage injection or restart",
    "prior_runtime_evidence": "docs/maintained-offline-tests.json; unchanged Collector harness and config hashes checked, not re-executed",
    "not_run": list(NOT_RUN),
    "retained_limits": [
        "Original expression excluded for missing rights clearance",
        "Private linked dependency documents excluded and unreviewed",
        "Installed client identity and effective configuration unknown",
        "Trusted synthetic identity values only; producer/config/policy matching not implemented by fixture classifier",
        "No production or native qualification",
    ],
}
COMMANDS = {
    "python3 tools/check_native_monitoring.py",
    "python3 tools/check_scaffold.py",
    "python3 -m unittest discover -s tests -p 'test_native_monitoring*.py' -v",
    "python3 -m unittest discover -s tests -p 'test_telemetry_evidence.py' -v",
    "python3 tools/validate.py",
    "PYTHONOPTIMIZE=1 python3 -O tools/validate.py",
    "git diff --check",
    "python -m ruff check --select E4,E7,E9,F,I tools/check_native_monitoring.py tests/test_native_monitoring.py",
    "python -m ruff format --check tools/check_native_monitoring.py tests/test_native_monitoring.py",
    "python -m mypy --strict --explicit-package-bases --follow-imports=silent --no-incremental tools/check_native_monitoring.py tests/test_native_monitoring.py",
}


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def object_value(value: JsonValue, label: str = "object") -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("expected object: " + label)
    return value


def array(value: JsonValue, label: str = "array") -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError("expected array: " + label)
    return value


def string(value: JsonValue, label: str = "string") -> str:
    if not isinstance(value, str):
        raise ValueError("expected string: " + label)
    return value


def integer(value: JsonValue, label: str = "integer") -> int:
    if type(value) is not int:
        raise ValueError("expected integer: " + label)
    return value


def read(root: Path, name: str) -> JsonObject:
    # JSON decoding is the only untyped input boundary; every use narrows its shape.
    return object_value(cast(JsonValue, json.loads((root / name).read_text())), name)


def expect_fields(data: JsonObject, expected: JsonObject, label: str) -> None:
    for key, value in expected.items():
        observed = data.get(key)
        require(
            type(observed) is type(value) and observed == value,
            "unsupported claim/reference in " + label + ": " + key,
        )


def validate_receipt(root: Path) -> None:
    receipt = read(root, "docs/native-monitoring-validation.json")
    expect_fields(receipt, RECEIPT_FIELDS, "validation receipt")
    require(
        set(receipt)
        == set(RECEIPT_FIELDS) | {"python", "machine", "commands", "strict_tools"},
        "unreviewed validation receipt field",
    )
    require(
        re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", string(receipt["python"])),
        "Python version",
    )
    require(
        string(receipt["machine"]) in {"x86_64", "aarch64", "arm64"}, "machine identity"
    )
    expect_fields(
        object_value(receipt["strict_tools"]),
        {"ruff": "0.14.5", "mypy": "1.18.2"},
        "strict tools",
    )
    commands = [object_value(c) for c in array(receipt["commands"])]
    names = [string(c["command"]) for c in commands]
    require(
        len(names) == len(COMMANDS) and set(names) == COMMANDS,
        "unreviewed validation command or missing offline check",
    )
    for command in commands:
        expect_fields(command, {"result": "passed"}, "validation command")
        name = string(command["command"])
        if name in {
            "python3 tools/validate.py",
            "PYTHONOPTIMIZE=1 python3 -O tools/validate.py",
        }:
            require(
                set(command)
                == {"command", "result", "tests", "passed", "skipped", "skip_reasons"},
                "aggregate receipt fields",
            )
            require(
                integer(command["tests"]) == integer(command["passed"]) + 13
                and integer(command["skipped"]) == 13
                and integer(command["passed"]) > 0,
                "aggregate test accounting",
            )
            expect_fields(
                command,
                {
                    "skip_reasons": [
                        "8 disposable Linux arm64 installation cases unavailable",
                        "5 prepared Temporal real-lane cases not requested",
                    ]
                },
                "skip scope",
            )
        elif "unittest discover" in name:
            require(
                set(command) == {"command", "result", "tests"}
                and integer(command["tests"]) > 0,
                "focused test accounting",
            )
        elif name == "python3 tools/check_native_monitoring.py":
            require(
                set(command) == {"command", "result", "source_units", "surfaces"},
                "checker fields",
            )
            expect_fields(
                command, {"source_units": 19, "surfaces": 14}, "checker accounting"
            )
        else:
            require(set(command) == {"command", "result"}, "unexpected check claim")


def validate(root: Path = ROOT) -> JsonObject:
    manifest = read(root, "telemetry/native-monitoring-lift.json")
    expect_fields(manifest, MANIFEST_FIELDS, "manifest")
    require(
        set(manifest) == set(MANIFEST_FIELDS) | {"source_units", "artifact_sha256"},
        "manifest fields",
    )
    hashes = object_value(manifest["artifact_sha256"])
    require(set(hashes) == ARTIFACTS, "incomplete or unexpected artifact inventory")
    for name, value in hashes.items():
        expected = string(value)
        path = root / name
        require(path.is_file() and not path.is_symlink(), "missing/linked artifact")
        require(path.resolve().is_relative_to(root.resolve()), "artifact path escaped")
        require(re.fullmatch(r"[0-9a-f]{64}", expected), "invalid digest")
        require(
            hashlib.sha256(path.read_bytes()).hexdigest() == expected,
            "artifact drift: " + name,
        )
    document = (root / "specs/native-monitoring.md").read_text()
    anchors = {
        line[3:].lower().replace(" ", "-")
        for line in document.splitlines()
        if line.startswith("## ")
    }
    units = [object_value(u) for u in array(manifest["source_units"])]
    require(
        [u["id"] for u in units] == [f"SRC-011-{i:02}" for i in range(1, 20)],
        "source accounting incomplete",
    )
    for unit in units:
        file, anchor = string(unit["destination"]).split("#")
        require(
            file == "specs/native-monitoring.md" and anchor in anchors,
            "missing section destination",
        )
        require(
            unit["disposition"]
            == "original_concept_adaptation_with_original_expression_and_private_dependency_exclusions",
            "rights exclusion removed",
        )
    matrix = read(root, "telemetry/native-monitoring-compatibility.json")
    expect_fields(
        matrix,
        {
            "schema_version": 1,
            "source_id": "SRC-011",
            "classification": "read_only_source_review_and_explicit_unknowns",
            "source_bytes_imported": False,
            "collector_dependencies": "telemetry/dependencies.json",
            "langfuse_dependency_status": "telemetry/langfuse-qualification.json",
        },
        "compatibility",
    )
    rows = [object_value(r) for r in array(matrix["surfaces"])]
    require(
        len(rows) == len(SURFACES)
        and {string(r["surface"]): r["status"] for r in rows} == SURFACES,
        "surface coverage/support changed",
    )
    sources = [object_value(s) for s in array(matrix["public_sources"])]
    expected_paths = {
        "schema": "codex-rs/core/config.schema.json",
        "version": "codex-rs/Cargo.toml",
        "provider": "codex-rs/otel/src/provider.rs",
        "initialization": "codex-rs/core/src/otel_init.rs",
        "cli": "codex-rs/exec/src/lib.rs",
        "app_server": "codex-rs/app-server/src/lib.rs",
        "events": "codex-rs/otel/src/events/session_telemetry.rs",
    }
    require(
        len(sources) == len(expected_paths)
        and {string(s["id"]): s["path"] for s in sources} == expected_paths,
        "source citation inventory changed",
    )
    for source in sources:
        require(
            source["repository"] == "openai/codex"
            and source["revision"] == PIN
            and re.fullmatch(r"[0-9a-f]{40}", string(source["git_blob_sha"]))
            and source["url"]
            == f"https://github.com/openai/codex/blob/{PIN}/{source['path']}",
            "unpinned or unexpected public citation",
        )
    require(
        {string(k) for k in array(matrix["configuration_keys"])}
        == {
            "otel." + key
            for key in (
                "environment",
                "exporter",
                "log_user_prompt",
                "metrics_exporter",
                "span_attributes",
                "trace_exporter",
                "tracestate",
            )
        }
        and matrix["http_protocols"] == ["binary", "json"],
        "unreviewed settings",
    )
    for row in rows:
        require(
            row["installed_qualified"] is False and row["live_acceptance"] == "not_run",
            "unverified runtime claim",
        )
        surface = string(row["surface"])
        if surface in {"cli", "app_server"}:
            require(
                row["version"] == "0.143.0"
                and row["source_revision"] == PIN
                and {string(x) for x in array(row["source_evidence"])}
                == {"schema", surface, "provider", "version"},
                "client pin/citations missing",
            )
        elif surface in {"collector_traces", "langfuse"}:
            file = "telemetry/" + (
                "dependencies.json"
                if surface == "collector_traces"
                else "langfuse-qualification.json"
            )
            dep = read(root, file)
            require(
                row["source_revision"] == dep["source_revision"]
                and row["version"] == dep["version"],
                "dependency pin drift",
            )
        else:
            require(
                row["source_revision"] is None and row["version"] is None,
                "unreviewed surface pin",
            )
    prior = read(root, string(manifest["prior_evidence"]))
    source_hashes = object_value(prior["source_hashes"])
    for name in (
        "tools/collector_privacy_check.py",
        "tools/collector_recovery_check.py",
        "tests/test_telemetry_evidence.py",
        "telemetry/collector.yaml",
    ):
        if name == "tools/collector_privacy_check.py":
            # Audited historical-to-current classifier transition, not a runtime
            # requalification. Never rewrite the historical receipt or admit an
            # arbitrary new harness merely because the manifest was rehashed.
            require(
                source_hashes[name]
                == "1207ceac36733a3578a64665f95531eebdd465fe5a35439f5eb9393c1d5c58d4"
                and hashes[name]
                == "4ccd22cb3bd80727cafd7256963480a6aaa806f3d8dfddeca57e2e76c999efa5",
                "prior source evidence drift",
            )
        else:
            require(source_hashes[name] == hashes[name], "prior source evidence drift")
    for key in ("Collector_privacy", "Collector_queue_recovery"):
        observed = object_value(object_value(prior[key])["observed"])
        require(
            observed["backend"] == "persisted_file_only", "prior evidence scope changed"
        )
    require(prior["live_acceptance"] == "not_run", "prior live claim changed")
    sources = [object_value(s) for s in array(read(root, "provenance.json")["sources"])]
    matching = [s for s in sources if s["source_id"] == "SRC-011"]
    require(len(matching) == 1, "provenance source count")
    expect_fields(matching[0], PROVENANCE_FIELDS, "provenance")
    status = object_value(read(root, "STATUS.json")["native_monitoring_document_lift"])
    require(set(status) == set(STATUS_FIELDS), "unexpected monitoring status field")
    expect_fields(status, STATUS_FIELDS, "status")
    validate_receipt(root)
    return {
        "status": "passed",
        "evidence_class": "offline_document_contract",
        "source_units": len(units),
        "surfaces": len(rows),
        "runtime_execution": "not_run",
        "current_harness_runtime_qualification": "not_run",
        "prior_runtime_evidence": "historical_only_classifier_changed",
        "live_acceptance": "not_run",
    }


if __name__ == "__main__":
    print(json.dumps(validate(), sort_keys=True))
