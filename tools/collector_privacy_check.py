#!/usr/bin/env python3
"""Actual standard Collector privacy challenge. Never a native telemetry uploader.

File exporter readback is an explicitly separate backend from Langfuse. A schema
URL leak blocks the proposed path and prevents backend/full H08 qualification.
"""

import argparse
import hashlib
import json
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON_IMAGE = (
    "python@sha256:0e58fc4a4575979bfce26735236422a48b38dae99d72b9d72a15e8e3fe0f7f93"
)
CANARY = "SYNTHETIC_PRIVACY_CANARY_17"


def command(
    *args: object, check: bool = True, timeout: float = 30
) -> subprocess.CompletedProcess[str]:
    arguments = list(map(str, args))
    automatic = arguments[0] == "run" and "--name" not in arguments
    container = "runspindle-command-" + uuid.uuid4().hex if automatic else None
    if automatic:
        arguments[1:1] = ["--name", container]
    try:
        return subprocess.run(
            ["docker", *arguments],
            check=check,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        if container:
            subprocess.run(
                ["docker", "rm", "--force", container], capture_output=True, timeout=30
            )
            if (
                subprocess.run(
                    ["docker", "inspect", container], capture_output=True, timeout=30
                ).returncode
                == 0
            ):
                raise RuntimeError("disposable command container cleanup incomplete")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def check(value, message):
    if not value:
        raise AssertionError(message)


def require_trace_acceptance(
    client: Callable[[str, str, str], str], host: str, payload: str
) -> None:
    script = Path(__file__).with_name("collector_trace_http.py").read_text()
    receipt = client(script, host, payload)
    if receipt not in ("accepted", "accepted_with_warning"):
        state = receipt if receipt in ("spans_rejected", "rejected") else "unknown"
        raise RuntimeError(f"full trace acceptance unconfirmed ({state})")


def fixture():
    return {
        "resourceSpans": [
            {
                "schemaUrl": "https://synthetic.invalid/" + CANARY,
                "resource": {
                    "attributes": [
                        {"key": key, "value": {"stringValue": value}}
                        for key, value in (
                            ("task_id", "task-17"),
                            ("session_id", "session-17"),
                            ("producer", "synthetic-otlp"),
                            ("config_sha256", "a" * 64),
                            ("policy_sha256", "b" * 64),
                            ("secret-path", CANARY),
                        )
                    ]
                },
                "scopeSpans": [
                    {
                        "schemaUrl": "https://synthetic.invalid/scope/" + CANARY,
                        "scope": {
                            "name": CANARY,
                            "version": CANARY,
                            "attributes": [
                                {"key": "secret", "value": {"stringValue": CANARY}}
                            ],
                        },
                        "spans": [
                            {
                                "traceId": "1" * 32,
                                "spanId": "2" * 16,
                                "name": CANARY,
                                "traceState": "private=" + CANARY,
                                "kind": 1,
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "1000001000",
                                "status": {"message": CANARY, "code": 2},
                                "events": [
                                    {
                                        "timeUnixNano": "1000000500",
                                        "name": CANARY,
                                        "attributes": [
                                            {
                                                "key": CANARY,
                                                "value": {"stringValue": CANARY},
                                            }
                                        ],
                                    }
                                ],
                                "links": [],
                                "attributes": [
                                    {
                                        "key": "event_id",
                                        "value": {"stringValue": "event-17"},
                                    },
                                    {
                                        "key": "tool_error",
                                        "value": {"stringValue": CANARY},
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def canary_paths(value: object, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        return [
            path
            for key, child in value.items()
            for path in (
                canary_paths(key, prefix + "/key")
                + canary_paths(child, prefix + "/" + key)
            )
        ]
    if isinstance(value, list):
        return [
            path
            for index, child in enumerate(value)
            for path in canary_paths(child, prefix + "/" + str(index))
        ]
    return [prefix] if isinstance(value, str) and CANARY in value else []


def identity_attributes(attributes: object) -> dict[str, str | None] | None:
    """Read unambiguous OTLP string identities, never last-value-wins evidence."""
    if not isinstance(attributes, list):
        return None
    values: dict[str, str | None] = {}
    for attribute in attributes:
        if not isinstance(attribute, dict) or set(attribute) != {"key", "value"}:
            return None
        key, value = attribute["key"], attribute["value"]
        if not isinstance(key, str) or not key or key in values:
            return None
        if not isinstance(value, dict):
            return None
        values[key] = (
            value["stringValue"]
            if set(value) == {"stringValue"} and isinstance(value["stringValue"], str)
            else None
        )
    return values


def object_list(value: object) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def outcome(http_accepted: object, records: object) -> dict[str, Any]:
    # Exact synthetic context is required independently of transport acceptance.
    # This is file-readback evidence only, not native source authentication.
    expected_resource = {
        "task_id": "task-17",
        "session_id": "session-17",
        "producer": "synthetic-otlp",
        "config_sha256": "a" * 64,
        "policy_sha256": "b" * 64,
    }
    matching = []
    for record in object_list(records):
        for resource in object_list(record.get("resourceSpans")):
            context = resource.get("resource")
            if not isinstance(context, dict):
                continue
            attrs = identity_attributes(context.get("attributes"))
            if attrs is None or any(
                attrs.get(key) != value for key, value in expected_resource.items()
            ):
                continue
            for scope in object_list(resource.get("scopeSpans")):
                for span in object_list(scope.get("spans")):
                    events = identity_attributes(span.get("attributes"))
                    if (
                        events is not None
                        and span.get("traceId") == "1" * 32
                        and span.get("spanId") == "2" * 16
                        and events.get("event_id") == "event-17"
                    ):
                        matching.append(span)
    return {
        "transport": "accepted" if http_accepted is True else "unknown",
        "backend": "persisted_file_only" if matching else "unknown",
        "matching_records": len(matching),
        "duplicates": max(len(matching) - 1, 0),
        "privacy": (
            "blocked"
            if canary_paths(records)
            else "passed_tested_synthetic_envelope_only"
        ),
    }


def verified_dependencies():
    pins = json.loads((ROOT / "telemetry/dependencies.json").read_bytes())
    check(
        pins["image"]
        == "otel/opentelemetry-collector-contrib@sha256:36ec8a5b77a11cb6d359378ef6103ce852fd82becec77a3bf5bb92eab5a818bf"
        and pins["target"] == "linux/arm64",
        "unsupported Collector target",
    )
    metadata = json.loads(command("image", "inspect", pins["image"]).stdout)[0]
    check(
        metadata["Architecture"] == "arm64"
        and metadata["Os"] == "linux"
        and metadata["Config"]["Labels"]["org.opencontainers.image.revision"]
        == pins["source_revision"],
        "Collector source/architecture binding mismatch",
    )
    sbom = (ROOT / "telemetry/collector-sbom.json").read_bytes()
    check(sha(sbom) == pins["sbom_sha256"], "upstream SBOM mismatch")
    check(
        sha((ROOT / "telemetry/collector-buildinfo.txt").read_bytes())
        == pins["buildinfo_sha256"],
        "dependency buildinfo mismatch",
    )
    check(
        all(
            p["checksums"] and p["license"] not in ("UNKNOWN", "NOASSERTION")
            for p in pins["dependencies"]
        ),
        "unqualified dependency closure",
    )
    for name, expected in pins["notices"].items():
        check(sha((ROOT / name).read_bytes()) == expected, "dependency notice mismatch")
    return pins


def run():
    pins = verified_dependencies()
    prefix = "runspindle-privacy-" + uuid.uuid4().hex
    network, collector = prefix + "-net", prefix + "-collector"
    clients = []
    with tempfile.TemporaryDirectory(prefix="collector-challenge-") as temporary:
        folder = Path(temporary)
        folder.chmod(0o777)
        source = ROOT / "telemetry/collector.yaml"
        config = source.read_bytes()
        proposed = config
        (folder / "proposed.yaml").write_bytes(proposed)
        schema_check = command(
            "run",
            "--rm",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "512m",
            "--cpus",
            "2",
            "--pids-limit",
            "128",
            "-v",
            str(folder / "proposed.yaml") + ":/config.yaml:ro",
            pins["image"],
            "validate",
            "--config=/config.yaml",
            check=False,
        )
        check(
            schema_check.returncode == 0,
            "standard privacy configuration rejected: " + schema_check.stderr,
        )
        try:
            command(
                "network",
                "create",
                "--internal",
                "--opt",
                "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                network,
            )
            command(
                "run",
                "--detach",
                "--pull",
                "never",
                "--name",
                collector,
                "--network",
                network,
                "--network-alias",
                "collector",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "512m",
                "--cpus",
                "2",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:rw,nosuid,size=64m",
                "-v",
                str(source) + ":/config.yaml:ro",
                "-v",
                str(folder) + ":/output",
                pins["image"],
                "--config=/config.yaml",
            )

            def client(code: str, *args: str) -> str:
                name = prefix + "-client-" + str(len(clients))
                clients.append(name)
                result = command(
                    "run",
                    "--name",
                    name,
                    "--pull",
                    "never",
                    "--network",
                    network,
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--memory",
                    "128m",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "64",
                    PYTHON_IMAGE,
                    "python",
                    "-c",
                    code,
                    *args,
                    check=False,
                )
                if result.returncode:
                    raise RuntimeError(
                        (result.stderr + command("logs", collector).stderr).replace(
                            CANARY, "[synthetic-canary]"
                        )
                    )
                return result.stdout.strip()

            # Readiness probe transmits no telemetry and is independent of export.
            ready = "import time,urllib.request; end=time.monotonic()+15\nwhile True:\n try:\n  urllib.request.urlopen('http://collector:13133',timeout=1);break\n except Exception:\n  if time.monotonic()>end:raise\n  time.sleep(.1)"
            client(ready)
            payload = json.dumps(fixture(), separators=(",", ":"))
            # Explicit duplicate event: synthetic delivery count is independently visible.
            for _ in range(2):
                require_trace_acceptance(client, "collector", payload)
            reject = "import urllib.request,urllib.error\ntry:\n urllib.request.urlopen(urllib.request.Request('http://collector:4318/v1/traces',data=b'{invalid-SYNTHETIC_PRIVACY_CANARY_17',headers={'Content-Type':'application/json','X-Synthetic-Secret':'SYNTHETIC_PRIVACY_CANARY_17'}),timeout=5)\nexcept urllib.error.HTTPError as e:\n print(e.code)"
            check(
                client(reject) == "400",
                "malformed envelope was not explicitly rejected",
            )
            path = folder / "observed.jsonl"
            deadline = time.monotonic() + 15
            records = []
            while time.monotonic() < deadline:
                try:
                    records = [
                        json.loads(line) for line in path.read_text().splitlines()
                    ]
                    if outcome(True, records)["matching_records"] == 2:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            observed = outcome(True, records)
            check(
                observed["matching_records"] == 2,
                "no independent correlated file readback",
            )
            leaks = sorted(set(canary_paths(records)))
            check(
                not leaks, "whole-envelope challenge leaked canary paths: " + str(leaks)
            )
            command("stop", "--time", "10", collector, timeout=15)
            check(
                path.is_file() and len(path.read_bytes()) > 0,
                "shutdown lost all file records",
            )
            logs = command("logs", collector)
            check(
                CANARY not in logs.stdout + logs.stderr,
                "internal error log leaked synthetic canary",
            )
            command("rm", collector)
            buffered = folder / "buffered"
            buffered.mkdir(mode=0o777)
            buffered.chmod(0o777)
            buffered_config = folder / "buffered.yaml"
            buffered_config.write_bytes(
                config.replace(b"flush_interval: 100ms", b"flush_interval: 60s")
            )
            command(
                "run",
                "--detach",
                "--pull",
                "never",
                "--name",
                collector,
                "--network",
                network,
                "--network-alias",
                "collector",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "512m",
                "--cpus",
                "2",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:rw,nosuid,size=64m",
                "-v",
                str(buffered_config) + ":/config.yaml:ro",
                "-v",
                str(buffered) + ":/output",
                pins["image"],
                "--config=/config.yaml",
            )
            client(ready)
            require_trace_acceptance(client, "collector", payload)
            command("kill", collector)
            output = buffered / "observed.jsonl"
            buffered_records = (
                [json.loads(line) for line in output.read_text().splitlines()]
                if output.exists()
                else []
            )
            buffered_outcome = outcome(True, buffered_records)
            check(
                buffered_outcome["matching_records"] in (0, 1),
                "unexpected buffered duplicate",
            )
            loss = (
                "lost_after_volatile_export_buffer_SIGKILL"
                if not buffered_records
                else "persisted_before_SIGKILL"
            )
            return {
                "status": "passed",
                "evidence_class": "isolated_privacy_challenge",
                "Collector": pins["version"],
                "config_sha256": sha(config),
                "proposed_config_sha256": sha(proposed),
                "dependencies_sha256": sha(
                    (ROOT / "telemetry/dependencies.json").read_bytes()
                ),
                "observed": observed,
                "malformed_envelope": "rejected_HTTP_400_zero_extra_backend_records",
                "internal_error_log_canaries": "absent",
                "buffered_interruption": {
                    "state": loss,
                    "matching_records": buffered_outcome["matching_records"],
                },
                "canary_paths": leaks,
                "schema_url_setter": "supported_and_canaries_removed_in_file_readback",
                "Langfuse_pipeline": "not_run",
                "native_producer": "blocked",
                "full_H08": "blocked",
                "live_acceptance": "not_run",
            }
        finally:
            for name in [*clients, collector]:
                command("rm", "--force", name, check=False)
            command("network", "rm", network, check=False)
            for name in [*clients, collector]:
                check(
                    command("inspect", name, check=False).returncode != 0,
                    "leaked privacy challenge process",
                )
            check(
                command("network", "inspect", network, check=False).returncode != 0,
                "leaked privacy challenge network",
            )


if __name__ == "__main__":

    def interrupted(*_):
        raise KeyboardInterrupt("Collector challenge cancelled")

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    results = [run(), run()]
    check(results[0] == results[1], "normalized privacy challenge outcomes changed")
    report = {**results[0], "repeat_runs": 2}
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    raise SystemExit(0)
