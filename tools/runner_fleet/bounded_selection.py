"""Read-only queued-job selection and existing-network configuration observations.

This module has no execution, registration, dispatch, setup, or promotion path.
Selection is not server-enforced assignment and observations are not traffic tests.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from typing import Any, Callable

OWNER = "newdarwindev"
REPOSITORIES = frozenset(
    "runspindle runspindle-core runspindle-host runspindle-browser runspindle-tether runspindle-refinery runspindle-console runspindle-platform".split()
)
LABELS = ["self-hosted", "linux", "x64", "runspindle-isolated", "strix"]
MAX_OUTPUT = 2 * 1024**2
READ_SECONDS = 15
Json = dict[str, Any]
Reader = Callable[[list[str]], str]


def read_command(argv: list[str]) -> str:
    """Bounded in-memory capture; callers below construct fixed read-only commands."""
    process = subprocess.Popen(
        [
            "/usr/bin/timeout",
            "--signal=TERM",
            "--kill-after=5s",
            str(READ_SECONDS) + "s",
            *argv,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = bytearray()
    error = bytearray()
    complete = False
    try:
        with selectors.DefaultSelector() as selector:
            if process.stdout is None or process.stderr is None:
                raise RuntimeError("Missing subprocess capture pipes")
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, error)
            deadline = time.monotonic() + READ_SECONDS
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Read-only observation timed out")
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk)
                        if len(output) + len(error) > MAX_OUTPUT:
                            raise ValueError("Observation exceeds capture bound")
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        complete = True
        if code in (124, 137):
            raise TimeoutError("Read-only observation timed out")
        if code:
            # Do not echo arbitrary stderr or account information into receipts.
            raise RuntimeError("Read-only observation command failed")
        return output.decode("utf-8")
    finally:
        if not complete:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def match(left: Json, right: Any, op: str = "==") -> Json:
    return {"match": {"op": op, "left": left, "right": right}}


def expected_nft() -> list[Json]:
    records: list[Json] = [{"table": {"family": "inet", "name": "runspindle_fleet"}}]
    for chain in ("input", "forward"):
        records.append(
            {
                "chain": {
                    "family": "inet",
                    "table": "runspindle_fleet",
                    "name": chain,
                    "type": "filter",
                    "hook": chain,
                    "prio": -20,
                    "policy": "accept",
                }
            }
        )

    def rule(chain: str, expressions: list[Json]) -> None:
        records.append(
            {
                "rule": {
                    "family": "inet",
                    "table": "runspindle_fleet",
                    "chain": chain,
                    "expr": expressions,
                }
            }
        )

    for slot in range(2):
        iface = match({"meta": {"key": "iifname"}}, f"rsvm{slot}")
        rule(
            "input",
            [iface, match({"meta": {"key": "nfproto"}}, "ipv6"), {"drop": None}],
        )
        rule(
            "input",
            [
                iface,
                match(
                    {"payload": {"protocol": "ip", "field": "saddr"}},
                    f"10.231.{slot}.2",
                    "!=",
                ),
                {"drop": None},
            ],
        )
        rule(
            "input",
            [
                iface,
                match({"ct": {"key": "state"}}, ["established", "related"], "in"),
                {"accept": None},
            ],
        )
        rule(
            "input",
            [
                iface,
                match(
                    {"payload": {"protocol": "ip", "field": "daddr"}},
                    f"10.231.{slot}.1",
                ),
                match({"payload": {"protocol": "tcp", "field": "dport"}}, 3128),
                {"accept": None},
            ],
        )
        rule("input", [iface, {"counter": None}, {"drop": None}])
    for direction in ("iifname", "oifname"):
        rule(
            "forward",
            [
                match({"meta": {"key": direction}}, {"set": ["rsvm0", "rsvm1"]}),
                {"counter": None},
                {"drop": None},
            ],
        )
    return records


def normalize_nft(value: Any) -> Any:
    if isinstance(value, list):
        return [normalize_nft(item) for item in value]
    if isinstance(value, dict):
        if "counter" in value:
            counter = value["counter"]
            if (
                set(value) != {"counter"}
                or not isinstance(counter, dict)
                or set(counter) != {"packets", "bytes"}
            ):
                raise ValueError("Unknown counter schema")
            if any(type(v) is not int or v < 0 for v in counter.values()):
                raise ValueError("Invalid counter")
            return {"counter": None}
        return {
            key: normalize_nft(item) for key, item in value.items() if key != "handle"
        }
    return value


def observe_network(reader: Reader = read_command) -> Json:
    payload = json.loads(
        reader(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/sbin/nft",
                "-j",
                "list",
                "table",
                "inet",
                "runspindle_fleet",
            ]
        )
    )
    records = payload.get("nftables")
    if (
        not isinstance(records, list)
        or len(records) != 16
        or set(records[0]) != {"metainfo"}
        or records[0]["metainfo"].get("json_schema_version") != 1
    ):
        raise ValueError("Unexpected nftables inventory")
    normalized = normalize_nft(records[1:])
    if normalized != expected_nft():
        raise ValueError("Dedicated firewall differs from expected configuration")
    for slot in range(2):
        rows = json.loads(
            reader(
                ["/usr/sbin/ip", "-j", "-d", "address", "show", "dev", f"rsvm{slot}"]
            )
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("TAP inventory ambiguous")
        row = rows[0]
        link = row.get("linkinfo", {})
        data = link.get("info_data", {})
        if (
            row.get("ifname") != f"rsvm{slot}"
            or "UP" not in row.get("flags", [])
            or row.get("master") is not None
        ):
            raise ValueError("TAP missing, down, or bridged")
        if (
            link.get("info_kind") != "tun"
            or data.get("type") != "tap"
            or data.get("user") != f"runspindle-vm{slot}"
            or data.get("persist") is not True
        ):
            raise ValueError("TAP ownership/type mismatch")
        ipv4 = [
            (a.get("local"), a.get("prefixlen"))
            for a in row.get("addr_info", [])
            if a.get("family") == "inet"
        ]
        if ipv4 != [(f"10.231.{slot}.1", 30)] or any(
            a.get("family") == "inet6" and a.get("scope") != "link"
            for a in row.get("addr_info", [])
        ):
            raise ValueError("TAP addressing mismatch")
    for service in (
        "runspindle-proxy-0.service",
        "runspindle-proxy-1.service",
        "runspindle-host-canary.service",
        "runspindle-lan-canary.service",
    ):
        raw = reader(
            ["/usr/bin/systemctl", "show", service, "--property=ActiveState,SubState"]
        )
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if values != {"ActiveState": "active", "SubState": "running"}:
            raise ValueError("Required existing service is not running")
    return {
        "configuration_observed": True,
        "firewall_sha256": hashlib.sha256(
            json.dumps(normalized, sort_keys=True).encode()
        ).hexdigest(),
        "traffic_tested": False,
        "service_bytes_attested": False,
        "changes": [],
    }


def number(value: Any) -> bool:
    return type(value) is int and value > 0


def select_job(
    repository: str,
    head: str,
    workflow: str,
    job_name: str,
    reader: Reader = read_command,
) -> Json:
    if repository not in REPOSITORIES or not re.fullmatch("[0-9a-f]{40}", head):
        raise ValueError("Unknown repository or source identity")
    if (
        not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", workflow)
        or not job_name
        or len(job_name) > 128
    ):
        raise ValueError("Invalid workflow/job selector")
    full = OWNER + "/" + repository

    def get(path: str) -> Json:
        result: Json = json.loads(
            reader(
                [
                    "/usr/bin/gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    "GET",
                    "repos/" + full + "/" + path,
                ]
            )
        )
        if not isinstance(result, dict):
            raise ValueError("Invalid GitHub response")
        return result

    ref = get("git/ref/heads/main")
    if ref.get("object", {}).get("sha") != head:
        raise ValueError("Requested source is not current main")
    result = get("actions/runs?status=queued&per_page=100")
    if (
        type(result.get("total_count")) is not int
        or result["total_count"] > 100
        or len(result.get("workflow_runs", [])) != result["total_count"]
    ):
        raise ValueError("Queue pagination/inventory is incomplete")
    candidates = []
    matching = [
        run
        for run in result["workflow_runs"]
        if run.get("head_sha") == head and run.get("path") == workflow
    ]
    if len(matching) != 1:
        raise ValueError("Expected exactly one current-source workflow run")
    for run in matching:
        if not number(run.get("id")) or not number(run.get("run_attempt")):
            raise ValueError("Invalid run identity")
        if (
            run.get("status") != "queued"
            or run.get("event") not in ("push", "workflow_dispatch")
            or run.get("head_branch") != "main"
            or run.get("head_repository", {}).get("full_name") != full
        ):
            raise ValueError("Untrusted or changed run")
        jobs = get(
            f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"
        )
        if (
            type(jobs.get("total_count")) is not int
            or jobs["total_count"] > 100
            or len(jobs.get("jobs", [])) != jobs["total_count"]
        ):
            raise ValueError("Job pagination/inventory is incomplete")
        for job in jobs["jobs"]:
            if job.get("name") != job_name:
                continue
            if (
                not number(job.get("id"))
                or job.get("run_id") != run["id"]
                or job.get("head_sha") != head
                or job.get("status") != "queued"
                or sorted(job.get("labels", [])) != sorted(LABELS)
                or type(job.get("runner_id")) is not int
                or job.get("runner_id") != 0
                or job.get("runner_name") not in (None, "")
            ):
                raise ValueError("Job identity, labels, or assignment changed")
            candidates.append(
                {
                    "repository": full,
                    "source_sha": head,
                    "workflow": workflow,
                    "run_id": run["id"],
                    "run_attempt": run["run_attempt"],
                    "job_id": job["id"],
                    "job_name": job_name,
                    "labels": LABELS.copy(),
                }
            )
    if len(candidates) != 1:
        raise ValueError("Selection must identify exactly one queued job")
    selected = candidates[0]
    content = get("contents/" + workflow + "?ref=" + head)
    if content.get("encoding") != "base64" or content.get("path") != workflow:
        raise ValueError("Workflow bytes unavailable")
    raw = base64.b64decode(content["content"], validate=False)
    if not 0 < len(raw) <= 1024**2:
        raise ValueError("Workflow size outside bounds")
    if hashlib.sha1(
        b"blob " + str(len(raw)).encode() + b"\0" + raw
    ).hexdigest() != content.get("sha"):
        raise ValueError("Workflow Git blob mismatch")
    selected["workflow_sha256"] = hashlib.sha256(raw).hexdigest()
    # Re-read exact mutable identities before issuing a non-authoritative snapshot.
    current = get(f"actions/runs/{selected['run_id']}")
    current_job = get(f"actions/jobs/{selected['job_id']}")
    if (
        current.get("id") != selected["run_id"]
        or current.get("event") not in ("push", "workflow_dispatch")
        or current.get("path") != workflow
        or current.get("head_branch") != "main"
        or current.get("head_repository", {}).get("full_name") != full
        or current_job.get("id") != selected["job_id"]
        or current_job.get("name") != job_name
        or sorted(current_job.get("labels", [])) != sorted(LABELS)
        or type(current_job.get("runner_id")) is not int
        or current_job.get("runner_name") not in (None, "")
        or current.get("run_attempt") != selected["run_attempt"]
        or current.get("head_sha") != head
        or current.get("status") != "queued"
        or current_job.get("status") != "queued"
        or current_job.get("runner_id") != 0
        or current_job.get("head_sha") != head
        or current_job.get("run_id") != selected["run_id"]
        or get("git/ref/heads/main").get("object", {}).get("sha") != head
    ):
        raise ValueError("Selection became stale")
    return selected


def inspect(
    repository: str,
    head: str,
    workflow: str,
    job_name: str,
    reader: Reader = read_command,
) -> Json:
    calls = 0
    deadline = time.monotonic() + 120

    def bounded_reader(argv: list[str]) -> str:
        nonlocal calls
        calls += 1
        if calls > 18 or time.monotonic() >= deadline:
            raise RuntimeError("Observation budget exhausted")
        result = reader(argv)
        if time.monotonic() >= deadline:
            raise TimeoutError("Observation budget exhausted")
        return result

    selected = select_job(repository, head, workflow, job_name, bounded_reader)
    network = observe_network(bounded_reader)
    prefix = "repos/" + OWNER + "/" + repository + "/"

    def reread(path: str) -> Json:
        value: Json = json.loads(
            bounded_reader(
                [
                    "/usr/bin/gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    "GET",
                    prefix + path,
                ]
            )
        )
        return value

    latest_run = reread(f"actions/runs/{selected['run_id']}")
    latest_job = reread(f"actions/jobs/{selected['job_id']}")
    latest_ref = reread("git/ref/heads/main")
    if (
        latest_run.get("run_attempt") != selected["run_attempt"]
        or latest_run.get("status") != "queued"
        or latest_run.get("head_sha") != head
        or latest_job.get("id") != selected["job_id"]
        or latest_job.get("run_id") != selected["run_id"]
        or latest_job.get("head_sha") != head
        or latest_job.get("status") != "queued"
        or latest_job.get("runner_id") != 0
        or type(latest_job.get("runner_id")) is not int
        or sorted(latest_job.get("labels", [])) != sorted(LABELS)
        or latest_ref.get("object", {}).get("sha") != head
    ):
        raise ValueError("Selection changed during network observation")
    return {
        "schema_version": 1,
        "status": "observed_only",
        "selected": selected,
        "network": network,
        "execution_allowed": False,
        "dispatches": 0,
        "registrations": 0,
        "attempt_limit_proposal": 1,
        "blockers": [
            "selection is not server-enforced assignment",
            "runtime service bytes and traffic isolation unverified",
            "bounded live writer/supervisor adapter not implemented",
            "fresh observations and explicit operational scope required before execution",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.execute:
        raise SystemExit("Execution unavailable; this is read-only observation")
    print(
        json.dumps(
            inspect(args.repository, args.source_sha, args.workflow, args.job_name),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
