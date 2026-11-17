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


class SchemaRejected(ValueError):
    """Durable capacity is unknown; do not repair or allocate from this store."""


SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS control(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS resources(\n                id TEXT PRIMARY KEY, slot INTEGER NOT NULL, repository TEXT NOT NULL,\n                requested_job INTEGER NOT NULL, runner_name TEXT NOT NULL UNIQUE,\n                phase TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,\n                details TEXT NOT NULL DEFAULT '{}')",
    "CREATE UNIQUE INDEX IF NOT EXISTS occupied_slot ON resources(slot)\n                WHERE phase != 'destroyed'",
    "CREATE UNIQUE INDEX IF NOT EXISTS occupied_job ON resources(repository, requested_job)\n                WHERE phase != 'destroyed'",
    "CREATE TABLE IF NOT EXISTS events(\n                id INTEGER PRIMARY KEY, at REAL NOT NULL, resource TEXT,\n                kind TEXT NOT NULL, details TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS mac_job_observations(\n                repository TEXT NOT NULL, job_id INTEGER NOT NULL,\n                run_id INTEGER NOT NULL, head_sha TEXT NOT NULL,\n                outcome TEXT NOT NULL, observed REAL NOT NULL,\n                PRIMARY KEY(repository, job_id))",
)


def _shape(db: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )
    ]


def _validate_schema(db: sqlite3.Connection) -> None:
    if (
        db.execute("PRAGMA user_version").fetchone()[0] != 0
        or db.execute("SELECT count(*) FROM sqlite_temp_master").fetchone()[0] != 0
    ):
        raise SchemaRejected("incompatible fleet state schema")
    expected = sqlite3.connect(":memory:")
    try:
        for statement in SCHEMA_STATEMENTS:
            expected.execute(statement)
        if _shape(db) != _shape(expected):
            raise SchemaRejected("incompatible fleet state schema")
    finally:
        expected.close()


class State:
    def __init__(self, directory: Any) -> None:
        os.umask(0o077)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        path = self.directory / "fleet.sqlite"
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            created = False
        else:
            os.close(descriptor)
            created = True
        self.db = sqlite3.connect(path, timeout=30)
        os.chmod(self.directory / "fleet.sqlite", 0o600)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if created and not _shape(self.db):
                for statement in SCHEMA_STATEMENTS:
                    self.db.execute(statement)
            _validate_schema(self.db)
            self.db.commit()
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

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
            _validate_schema(self.db)
            yield
            _validate_schema(self.db)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key: Any, default: Any = None) -> Any:
        _validate_schema(self.db)
        row = self.db.execute(
            "SELECT value FROM control WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: Any, value: Any) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT OR REPLACE INTO control VALUES(?,?)", (key, json.dumps(value))
            )

    def active(self) -> list[dict[str, Any]]:
        _validate_schema(self.db)
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
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if capacity not in (1, 2):
            raise ValueError("Fleet capacity must be one Mac slot or two Strix slots")
        if capacity == 2 and binding is not None:
            raise ValueError("Strix reservation cannot use a Mac binding")
        if capacity == 1 and (
            details is not None
            or not isinstance(binding, dict)
            or binding.get("host") != "macbook"
        ):
            raise ValueError("One-slot reservation requires a Mac host binding")
        with self.transaction():
            occupied = self.active()
            if len(occupied) >= capacity:
                raise RuntimeError("Capacity exhausted, including uncertain resources")
            if capacity == 1:
                if binding is None:
                    raise ValueError("One-slot reservation requires a Mac host binding")
                epoch = self.get("mac_lease_epoch", 0) + 1
                self.db.execute(
                    "INSERT OR REPLACE INTO control VALUES(?,?)",
                    ("mac_lease_epoch", json.dumps(epoch)),
                )
                binding = binding | {"lease_epoch": epoch}
            slot = next(
                s for s in range(capacity) if s not in {r["slot"] for r in occupied}
            )
            identity = uuid.uuid4().hex
            name = "runspindle-fleet-" + identity
            now = time.time()
            record_details = (
                (details or {})
                | (binding or {})
                | {
                    "requested_source": {
                        key: job.get(key)
                        for key in ("head_sha", "run_id", "event", "head_branch")
                    }
                }
            )
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
                    json.dumps(record_details),
                ),
            )
            self._event(
                identity,
                "reserved",
                {"job": job, "slot": slot, "binding": binding or {}},
            )
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

    def mac_job_observation(
        self, repository: str, job_id: int
    ) -> dict[str, Any] | None:
        _validate_schema(self.db)
        row = self.db.execute(
            "SELECT * FROM mac_job_observations WHERE repository=? AND job_id=?",
            (repository, job_id),
        ).fetchone()
        return dict(row) if row else None

    def record_mac_job(
        self, repository: str, job_id: int, run_id: int, head_sha: str, outcome: str
    ) -> None:
        if outcome not in ("completed", "queue_expired"):
            raise ValueError("Unsupported Mac job observation")
        with self.transaction():
            previous = self.mac_job_observation(repository, job_id)
            if previous and (previous["run_id"], previous["head_sha"]) != (
                run_id,
                head_sha,
            ):
                raise RuntimeError("Mac job identity changed across observations")
            if (
                previous
                and previous["outcome"] == "completed"
                and outcome != "completed"
            ):
                raise RuntimeError("Terminal Mac job observation cannot regress")
            self.db.execute(
                "INSERT OR REPLACE INTO mac_job_observations VALUES(?,?,?,?,?,?)",
                (repository, job_id, run_id, head_sha, outcome, time.time()),
            )
