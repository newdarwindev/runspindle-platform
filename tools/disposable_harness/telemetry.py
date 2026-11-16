"""Synthetic OTLP JSON delivery with independent SQLite backend readback.

The allowlist below is a synthetic-fixture policy, not a Collector processor or
an upstream OTLP implementation. HTTP-like acceptance never implies persistence.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from tools import collector_privacy_check as collector
from tools.disposable_harness.contracts import canonical, digest, parse, require
from tools.disposable_harness.runtime import DisposableRun

# Typed boundary to the existing diagnostic oracle; implementation stays shared.
CANARY = collector.CANARY
fixture: Callable[[], dict[str, Any]] = collector.fixture
canary_paths: Callable[[Any], list[str]] = collector.canary_paths
identity_attributes: Callable[[Any], dict[str, Any] | None] = (
    collector.identity_attributes
)

RESOURCE_KEYS = (
    "task_id",
    "session_id",
    "producer",
    "config_sha256",
    "policy_sha256",
    "producer_version",
)


def attributes(values: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": k, "value": {"stringValue": v}} for k, v in values.items()]


def produce(seed: int, number: int = 1) -> dict[str, Any]:
    require(type(seed) is int and 0 <= seed < 2**32, "seed bound")
    require(type(number) is int and 1 <= number <= 100, "event bound")
    value = copy.deepcopy(fixture())
    resource = value["resourceSpans"][0]
    context = {
        "task_id": f"task-{seed}",
        "session_id": f"session-{seed}",
        "producer": "synthetic-otlp",
        "config_sha256": digest(b"synthetic-config-v1"),
        "policy_sha256": digest(b"synthetic-allowlist-v1"),
        "producer_version": "synthetic-v1",
    }
    resource["resource"]["attributes"] = attributes(context) + attributes(
        {"path": CANARY}
    )
    span = resource["scopeSpans"][0]["spans"][0]
    span["traceId"] = digest(f"trace-{seed}".encode())[:32]
    span["spanId"] = digest(f"span-{seed}-{number}".encode())[:16]
    span["attributes"] = attributes(
        {"event_id": f"event-{seed}-{number}", "tool_error": CANARY}
    )
    span["links"] = [
        {
            "traceId": "3" * 32,
            "spanId": "4" * 16,
            "traceState": CANARY,
            "attributes": attributes({CANARY: CANARY}),
        }
    ]
    value["metadata"] = {"authorization": CANARY, "path": CANARY, CANARY: "private"}
    return value


def sanitize(value: dict[str, Any]) -> dict[str, Any]:
    """Allowlist known synthetic identity fields; reject ambiguous required values."""
    resource = value["resourceSpans"][0]
    span = resource["scopeSpans"][0]["spans"][0]
    context = identity_attributes(resource["resource"]["attributes"])
    events = identity_attributes(span["attributes"])
    if context is None or events is None:
        raise ValueError("ambiguous identity")
    require(
        all(isinstance(context[k], str) and context[k] for k in RESOURCE_KEYS),
        "missing context",
    )
    require(
        isinstance(events["event_id"], str) and events["event_id"],
        "missing event identity",
    )
    result = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": attributes({k: context[k] for k in RESOURCE_KEYS})
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "synthetic", "version": "1"},
                        "spans": [
                            {
                                "traceId": span["traceId"],
                                "spanId": span["spanId"],
                                "name": "synthetic-work",
                                "attributes": attributes(
                                    {"event_id": events["event_id"]}
                                ),
                            }
                        ],
                    }
                ],
            }
        ],
    }
    require(not canary_paths(result), "identity canary rejected")
    return result


def event_id(envelope: dict[str, Any]) -> str:
    result = envelope["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"][0][
        "value"
    ]["stringValue"]
    require(isinstance(result, str), "event identity")
    return str(result)


class Backend:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.mode = "persist"
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS records (body BLOB NOT NULL)")
        self.db.commit()

    def receive(self, payload: bytes) -> tuple[int, bytes]:
        if self.mode == "unavailable":
            raise ConnectionError("synthetic backend unavailable")
        if self.mode == "reject":
            return 400, b"{}"
        try:
            value = parse(payload)
            require(
                canonical(sanitize(value)) == canonical(value), "malformed OTLP fixture"
            )
        except (ValueError, KeyError, TypeError, IndexError):
            return 400, b"{}"
        if self.mode == "persist":
            require(
                self.db.execute("SELECT count(*) FROM records").fetchone()[0] < 100,
                "backend record bound",
            )
            self.db.execute("INSERT INTO records VALUES (?)", (payload,))
            self.db.commit()
        else:
            require(self.mode == "accept_only", "unknown backend mode")
        return 200, b"{}"

    def close(self) -> None:
        self.db.close()


class Pipeline:
    def __init__(self, run: DisposableRun, endpoint: str, spool: Path) -> None:
        self.run, self.endpoint = run, endpoint
        self.db = sqlite3.connect(spool)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, body BLOB, state TEXT, units INTEGER)"
        )
        self.db.commit()
        self.closed = False

    def emit(self, value: dict[str, Any], *, units: int = 1) -> str:
        require(not self.closed, "pipeline closed")
        self.run.budget()
        require(type(units) is int and 0 <= units <= 100, "usage bound")
        clean = sanitize(value)
        identity = event_id(clean)
        prior = self.db.execute(
            "SELECT body, units FROM events WHERE id=?", (identity,)
        ).fetchone()
        body = canonical(clean)
        if prior is not None:
            require(prior == (body, units), "conflicting duplicate identity")
            return identity
        require(
            self.db.execute("SELECT count(*) FROM events").fetchone()[0] < 32,
            "queue resource bound",
        )
        self.db.execute(
            "INSERT INTO events VALUES (?, ?, 'queued', ?)", (identity, body, units)
        )
        self.db.commit()
        return identity

    def flush(self, *, interrupt_after_accept: bool = False) -> None:
        require(not self.closed, "pipeline closed")
        for identity, body in self.db.execute(
            "SELECT id, body FROM events WHERE state='queued' ORDER BY id"
        ).fetchall():
            try:
                code, _ = self.run.request(self.endpoint, body)
            except (ConnectionError, TimeoutError):
                continue
            if code == 200 and interrupt_after_accept:
                raise InterruptedError("synthetic lost acknowledgement")
            require(code in (200, 400, 401, 403), "unexpected response remains unknown")
            self.db.execute(
                "UPDATE events SET state=? WHERE id=?",
                ("accepted" if code == 200 else "rejected", identity),
            )
            self.db.commit()

    def receipt(self, backend_path: Path) -> dict[str, Any]:
        """Read through a NEW read-only connection, independent of writer state."""
        records: list[Any] = []
        if backend_path.is_file():
            with closing(
                sqlite3.connect(backend_path.as_uri() + "?mode=ro", uri=True)
            ) as reader:
                records = [
                    json.loads(row[0])
                    for row in reader.execute("SELECT body FROM records")
                ]
        rows = self.db.execute(
            "SELECT id, body, state, units FROM events ORDER BY id"
        ).fetchall()
        observations = []
        usage = 0
        for identity, body, state, units in rows:
            # Bind every context field, including version/config/policy; partial
            # IDs or HTTP status alone cannot qualify a persisted event.
            matches = sum(canonical(record) == body for record in records)
            if matches:
                usage += units
            observations.append(
                {
                    "event_id": identity,
                    "delivery": state,
                    "persisted": bool(matches),
                    "copies": matches,
                    "duplicates": max(matches - 1, 0),
                    "loss": "accepted_without_readback"
                    if state == "accepted" and not matches
                    else "not_observed",
                }
            )
        return {
            "evidence_class": "synthetic_sqlite_readback",
            "emitted": len(rows),
            "queued": sum(row[2] == "queued" for row in rows),
            "rejected": sum(row[2] == "rejected" for row in rows),
            "accepted": sum(row[2] == "accepted" for row in rows),
            "persisted": sum(item["persisted"] for item in observations),
            "usage_units": usage,
            "events": observations,
            "privacy": "blocked" if canary_paths(records) else "passed_synthetic_only",
        }

    def shutdown(self) -> None:
        try:
            self.flush()
        finally:
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.db.close()
            self.closed = True
