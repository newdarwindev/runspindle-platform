#!/usr/bin/env python3
"""Two standard Collectors: durable exporter queue to a file-readback backend.

This tests actual Collector persistence/retry, not Langfuse compatibility. No
custom receiver, native uploader or replacement product backend is introduced.
"""
import argparse
import json
from pathlib import Path
import tempfile
import time
import uuid
import signal

from collector_privacy_check import (
    ROOT,
    PYTHON_IMAGE,
    CANARY,
    fixture,
    outcome,
    check,
    command,
    sha,
    verified_dependencies,
)


def run():
    pins = verified_dependencies()
    name = "runspindle-export-" + uuid.uuid4().hex
    network, primary, backend = name + "-net", name + "-primary", name + "-backend"
    clients = []
    with tempfile.TemporaryDirectory(prefix="collector-export-") as temporary:
        root = Path(temporary)
        root.chmod(0o777)
        for folder in ("primary", "backend"):
            (root / folder).mkdir(mode=0o777)
            (root / folder).chmod(0o777)
        baseline = (ROOT / "telemetry/collector.yaml").read_text()
        exporter = """exporters:
  otlphttp/backend:
    endpoint: http://backend:4318
    retry_on_failure:
      enabled: true
      initial_interval: 100ms
      max_interval: 1s
      max_elapsed_time: 60s
    sending_queue:
      enabled: true
      queue_size: 10
      storage: file_storage/queue
"""
        start, end = baseline.index("exporters:"), baseline.index("extensions:")
        config = baseline[:start] + exporter + baseline[end:]
        config = config.replace(
            "extensions:\n",
            "extensions:\n  file_storage/queue:\n    directory: /output/queue\n    create_directory: true\n",
        )
        config = config.replace(
            "extensions: [health_check]",
            "extensions: [health_check, file_storage/queue]",
        )
        config = config.replace("exporters: [file]", "exporters: [otlphttp/backend]")
        (root / "primary.yaml").write_text(config)
        (root / "backend.yaml").write_text(baseline)

        def spawn(container, alias, config_path, output):
            return command(
                "run",
                "--detach",
                "--pull",
                "never",
                "--name",
                container,
                "--network",
                network,
                "--network-alias",
                alias,
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
                str(config_path) + ":/config.yaml:ro",
                "-v",
                str(output) + ":/output",
                pins["image"],
                "--config=/config.yaml",
            )

        def client(code, *args):
            client_name = name + "-client-" + str(len(clients))
            clients.append(client_name)
            result = command(
                "run",
                "--name",
                client_name,
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
                raise RuntimeError(result.stderr.replace(CANARY, "[synthetic-canary]"))
            return result.stdout.strip()

        def ready(alias):
            code = "import time,urllib.request,sys; end=time.monotonic()+15\nwhile True:\n try:\n  urllib.request.urlopen('http://'+sys.argv[1]+':13133',timeout=1);break\n except Exception:\n  if time.monotonic()>end:raise\n  time.sleep(.1)"
            client(code, alias)

        def readback():
            path = root / "backend/observed.jsonl"
            try:
                return [json.loads(line) for line in path.read_text().splitlines()]
            except (OSError, ValueError):
                return []

        try:
            command(
                "network",
                "create",
                "--internal",
                "--opt",
                "com.docker.network.bridge.gateway_mode_ipv4=isolated",
                network,
            )
            spawn(primary, "primary", root / "primary.yaml", root / "primary")
            ready("primary")
            post = "import sys,urllib.request; r=urllib.request.urlopen(urllib.request.Request('http://primary:4318/v1/traces',data=sys.argv[1].encode(),headers={'Content-Type':'application/json'}),timeout=5);print(r.status)"
            check(
                client(post, json.dumps(fixture())) == "200",
                "synthetic event not accepted",
            )
            check(not readback(), "unavailable backend fabricated persistence")
            storage = root / "primary/queue"
            check(
                any(p.is_file() and p.stat().st_size for p in storage.rglob("*")),
                "durable sending queue did not create storage",
            )
            check(
                all(
                    CANARY.encode() not in p.read_bytes()
                    for p in storage.rglob("*")
                    if p.is_file()
                ),
                "queue contains synthetic secret canary",
            )
            # Abrupt interruption preserves the actual file_storage queue.
            command("kill", primary)
            command("rm", primary)
            spawn(backend, "backend", root / "backend.yaml", root / "backend")
            ready("backend")
            spawn(primary, "primary", root / "primary.yaml", root / "primary")
            ready("primary")
            deadline = time.monotonic() + 25
            records = []
            while time.monotonic() < deadline:
                records = readback()
                if outcome(True, records)["matching_records"]:
                    break
                time.sleep(0.1)
            observed = outcome(True, records)
            check(
                observed["matching_records"] == 1 and observed["privacy"] != "blocked",
                "durable queue recovery/readback mismatch",
            )
            command("stop", "--time", "10", primary, timeout=15)
            command("stop", "--time", "10", backend, timeout=15)
            return {
                "status": "passed",
                "evidence_class": "isolated_standard_Collector_queue_recovery",
                "Collector": pins["version"],
                "primary_config_sha256": sha(config.encode()),
                "backend_config_sha256": sha(baseline.encode()),
                "before_backend_readback": "queued_not_persisted",
                "recovery": "fresh_Collector_after_SIGKILL_with_file_storage_queue",
                "observed": observed,
                "Langfuse": "blocked_dependency_preparation",
                "native": "blocked",
                "full_H08": "blocked",
            }
        finally:
            for container in [*clients, primary, backend]:
                command("rm", "--force", container, check=False)
            command("network", "rm", network, check=False)
            check(
                all(
                    command("inspect", c, check=False).returncode != 0
                    for c in [*clients, primary, backend]
                ),
                "leaked Collector recovery process",
            )
            check(
                command("network", "inspect", network, check=False).returncode != 0,
                "leaked Collector recovery network",
            )


if __name__ == "__main__":

    def interrupted(*_):
        raise KeyboardInterrupt("Collector recovery cancelled")

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    first, second = run(), run()
    check(first == second, "normalized queue recovery changed")
    report = {**first, "repeat_runs": 2}
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
