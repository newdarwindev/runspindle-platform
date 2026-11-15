"""Durable capacity reservations. Unknown and cleaning resources remain occupied."""

import fcntl
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class State:
    def __init__(self, directory: Any) -> None:
        os.umask(0o077)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.db = sqlite3.connect(self.directory / "fleet.sqlite", timeout=30)
        os.chmod(self.directory / "fleet.sqlite", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resources(
                id TEXT PRIMARY KEY, slot INTEGER NOT NULL, repository TEXT NOT NULL,
                requested_job INTEGER NOT NULL, runner_name TEXT NOT NULL UNIQUE,
                phase TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                details TEXT NOT NULL DEFAULT '{}');
            CREATE UNIQUE INDEX IF NOT EXISTS occupied_slot ON resources(slot)
                WHERE phase != 'destroyed';
            CREATE UNIQUE INDEX IF NOT EXISTS occupied_job ON resources(repository, requested_job)
                WHERE phase != 'destroyed';
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY, at REAL NOT NULL, resource TEXT,
                kind TEXT NOT NULL, details TEXT NOT NULL);
        """)

    @contextmanager
    def lock(self) -> Iterator[None]:
        with (self.directory / "manager.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key: Any, default: Any = None) -> Any:
        row = self.db.execute(
            "SELECT value FROM control WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: Any, value: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO control VALUES(?,?)", (key, json.dumps(value))
            )

    def active(self) -> list[dict[str, Any]]:
        return [
            dict(row) | {"details": json.loads(row["details"])}
            for row in self.db.execute(
                "SELECT * FROM resources WHERE phase != 'destroyed' ORDER BY created"
            )
        ]

    def reserve(
        self,
        job: dict[str, Any],
        capacity: int = 2,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if capacity != 2:
            raise ValueError("Strix capacity must be exactly two")
        with self.transaction():
            occupied = self.active()
            if len(occupied) >= capacity:
                raise RuntimeError("Capacity exhausted, including uncertain resources")
            slot = next(
                s for s in range(capacity) if s not in {r["slot"] for r in occupied}
            )
            identity = uuid.uuid4().hex
            name = "runspindle-fleet-" + identity
            now = time.time()
            self.db.execute(
                "INSERT INTO resources VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    slot,
                    job["repository"],
                    job["id"],
                    name,
                    "reserved",
                    now,
                    now,
                    json.dumps(
                        (details or {})
                        | {
                            "requested_source": {
                                key: job.get(key)
                                for key in (
                                    "head_sha",
                                    "run_id",
                                    "event",
                                    "head_branch",
                                )
                            }
                        }
                    ),
                ),
            )
            self._event(identity, "reserved", {"job": job, "slot": slot})
        return next(r for r in self.active() if r["id"] == identity)

    def _event(self, identity: Any, kind: Any, details: Any) -> None:
        self.db.execute(
            "INSERT INTO events(at,resource,kind,details) VALUES(?,?,?,?)",
            (time.time(), identity, kind, json.dumps(details, sort_keys=True)),
        )

    def update(self, resource: dict[str, Any], phase: str, **details: Any) -> None:
        with self.transaction():
            row = self.db.execute(
                "SELECT details FROM resources WHERE id=?", (resource["id"],)
            ).fetchone()
            merged = json.loads(row[0]) | details
            self.db.execute(
                "UPDATE resources SET phase=?,updated=?,details=? WHERE id=?",
                (phase, time.time(), json.dumps(merged), resource["id"]),
            )
            self._event(resource["id"], phase, details)
