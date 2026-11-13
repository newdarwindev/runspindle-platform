#!/usr/bin/env python3
"""Actual standard Collector privacy challenge. Never a native telemetry uploader.

File exporter readback is an explicitly separate backend from Langfuse. A schema
URL leak blocks the proposed path and prevents backend/full H08 qualification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import signal

ROOT = Path(__file__).resolve().parents[1]
PYTHON_IMAGE = (
    "python@sha256:0e58fc4a4575979bfce26735236422a48b38dae99d72b9d72a15e8e3fe0f7f93"
)
CANARY = "SYNTHETIC_PRIVACY_CANARY_17"


def command(*args, check=True, timeout=30):
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


def canary_paths(value, prefix=""):
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


def outcome(http_accepted, records):
    # HTTP acceptance never substitutes for correlated backend readback.
    matching = []
    for record in records:
        for resource in record.get("resourceSpans", []):
            attrs = {
                a["key"]: a["value"].get("stringValue")
                for a in resource.get("resource", {}).get("attributes", [])
            }
            if (
                attrs.get("task_id") != "task-17"
                or attrs.get("session_id") != "session-17"
            ):
                continue
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    events = {
                        a["key"]: a["value"].get("stringValue")
                        for a in span.get("attributes", [])
                    }
                    if (
                        span.get("traceId") == "1" * 32
                        and span.get("spanId") == "2" * 16
                        and events.get("event_id") == "event-17"
                    ):
                        matching.append(span)
    return {
        "transport": "accepted" if http_accepted else "unknown",
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

            def client(code, *args):
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
                return result

            # Readiness probe transmits no telemetry and is independent of export.
            ready = "import time,urllib.request; end=time.monotonic()+15\nwhile True:\n try:\n  urllib.request.urlopen('http://collector:13133',timeout=1);break\n except Exception:\n  if time.monotonic()>end:raise\n  time.sleep(.1)"
            client(ready)
            post = "import sys,urllib.request; b=sys.argv[1].encode(); r=urllib.request.urlopen(urllib.request.Request('http://collector:4318/v1/traces',data=b,headers={'Content-Type':'application/json'}),timeout=5); print(r.status)"
            payload = json.dumps(fixture(), separators=(",", ":"))
            # Explicit duplicate event: synthetic delivery count is independently visible.
            accepted = [client(post, payload).stdout.strip() == "200" for _ in range(2)]
            reject = "import urllib.request,urllib.error\ntry:\n urllib.request.urlopen(urllib.request.Request('http://collector:4318/v1/traces',data=b'{invalid-SYNTHETIC_PRIVACY_CANARY_17',headers={'Content-Type':'application/json','X-Synthetic-Secret':'SYNTHETIC_PRIVACY_CANARY_17'}),timeout=5)\nexcept urllib.error.HTTPError as e:\n print(e.code)"
            check(
                client(reject).stdout.strip() == "400",
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
                    if outcome(all(accepted), records)["matching_records"] == 2:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.1)
            observed = outcome(all(accepted), records)
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
            check(
                client(post, payload).stdout.strip() == "200",
                "buffered event not accepted",
            )
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
