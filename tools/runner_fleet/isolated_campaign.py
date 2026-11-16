"""Offline campaign safety kernel. No GitHub, guest or service adapter is enabled."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .model import REPOSITORIES

GIB = 1024**3
# Conservative accounting allowances, not an enforced QEMU/host I/O quota.
DISK_BYTES = 2 * (24 * GIB + 64 * 1024**2)
METADATA_BYTES = 16 * 1024**2
CAMPAIGN_BYTES = 64 * 1024**2
UNSUPPORTED = "Exact pre-execution GitHub assignment enforcement is unavailable; live execution disabled"


class Blocked(RuntimeError):
    """A preserved stop condition; no implicit retry or reconciliation."""


def integer(value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid integer bound")
    return value


def hexadecimal(value: Any, width: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % width, value):
        raise ValueError("Exact source or digest required")
    return value


@dataclass(frozen=True)
class Target:
    repository: str
    ref: str
    source_sha: str
    workflow_sha256: str
    run_id: int
    run_attempt: int
    job_id: int

    def validate(self) -> None:
        if self.repository not in ["newdarwindev/" + r for r in REPOSITORIES]:
            raise ValueError("Repository outside the eight-repository scope")
        if not re.fullmatch(r"refs/heads/(main|ci/[A-Za-z0-9_./-]+)", self.ref):
            raise ValueError("Explicit main or ci branch required")
        if any(part in ("", ".", "..") for part in self.ref.split("/")):
            raise ValueError("Unsafe ref")
        hexadecimal(self.source_sha, 40)
        hexadecimal(self.workflow_sha256, 64)
        for value in (self.run_id, self.run_attempt, self.job_id):
            integer(value, 1, 2**63 - 1)

    def expected(self, label: str) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "head_repository": self.repository,
            "ref": self.ref,
            "head_sha": self.source_sha,
            "workflow_path": ".github/workflows/fleet-qualification.yml",
            "workflow_sha256": self.workflow_sha256,
            "event": "workflow_dispatch",
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "id": self.job_id,
            "name": "strix-linux-x64",
            "labels": ["self-hosted", "linux", "x64", label],
        }


@dataclass(frozen=True)
class Plan:
    targets: tuple[Target, ...]
    campaign_label: str
    image_sha256: str
    image_source_sha: str
    image_controller_sha256: str
    recipe_sha256: str
    tool_closure_sha256: str
    execution_source_sha: str
    execution_controller_sha256: str
    max_attempts: int = 8
    concurrency: int = 2
    log_bytes_per_attempt: int = 256 * 1024**2
    reserve_bytes: int = 30 * GIB
    admission_seconds: int = 1200
    job_seconds: int = 900
    cleanup_seconds: int = 300

    @classmethod
    def parse(cls, value: dict[str, Any]) -> Plan:
        if not isinstance(value, dict) or not isinstance(value.get("targets"), list):
            raise ValueError("Explicit targets required")
        try:
            result = cls(
                **(value | {"targets": tuple(Target(**t) for t in value["targets"])})
            )
        except TypeError as error:
            raise ValueError("Invalid or unknown plan fields") from error
        result.validate()
        return result

    def validate(self) -> None:
        if len(self.targets) != 8 or {t.repository for t in self.targets} != {
            "newdarwindev/" + r for r in REPOSITORIES
        }:
            raise ValueError("Exactly one target per repository required")
        for target in self.targets:
            target.validate()
        if not isinstance(self.campaign_label, str) or not re.fullmatch(
            r"runspindle-campaign-[0-9a-f]{32}", self.campaign_label
        ):
            raise ValueError(
                "Unique campaign label required; shared Strix labels forbidden"
            )
        for name in ("image_source_sha", "execution_source_sha"):
            hexadecimal(getattr(self, name), 40)
        for name in (
            "image_sha256",
            "image_controller_sha256",
            "recipe_sha256",
            "tool_closure_sha256",
            "execution_controller_sha256",
        ):
            hexadecimal(getattr(self, name), 64)
        integer(self.max_attempts, 1, 8)
        integer(self.concurrency, 1, 2)
        integer(self.log_bytes_per_attempt, 16 * 1024**2, GIB)
        integer(self.reserve_bytes, 30 * GIB, 1024 * GIB)
        integer(self.admission_seconds, 1, 1200)
        integer(self.job_seconds, 1, 900)
        integer(self.cleanup_seconds, 1, 300)

    @property
    def per_attempt(self) -> int:
        return DISK_BYTES + METADATA_BYTES + self.log_bytes_per_attempt

    @property
    def allocation(self) -> int:
        return CAMPAIGN_BYTES + self.max_attempts * self.per_attempt

    def encoded(self) -> str:
        self.validate()
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def execution_gate() -> None:
    # A label, observation, operator assertion or green synthetic test cannot
    # supply server-enforced pre-execution run binding. No override exists.
    raise Blocked(UNSUPPORTED)


class Ledger:
    """Separate offline policy ledger; never opens the fleet's State database.

    Callers must supply independent observations. There is intentionally no live
    adapter: bookkeeping alone cannot fence external disk writers or assignment.
    """

    def __init__(
        self, path: Path, plan: Plan, clock: Callable[[], int] | None = None
    ) -> None:
        self.path, self.plan = Path(path), plan
        self.clock = clock or time.time_ns
        plan.validate()
        if self.path.name == "fleet.sqlite":
            raise Blocked("Fleet ledger is outside the offline kernel scope")
        if self.path.is_symlink() or not self.path.is_file():
            raise Blocked("Existing private campaign ledger required")
        self.private_directory(self.path.parent)
        info = self.path.stat()
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise Blocked("Unsafe ledger ownership/mode/link count")
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            stored = self.db.execute(
                "SELECT plan FROM campaign WHERE singleton=1"
            ).fetchone()
            if stored is None or stored[0] != plan.encoded():
                raise Blocked("Plan changed across restart")
        except BaseException:
            self.db.close()
            raise

    @classmethod
    def create(cls, path: Path, plan: Plan, free_bytes: int) -> Ledger:
        plan.validate()
        if Path(path).name == "fleet.sqlite":
            raise Blocked("Fleet ledger is outside the offline kernel scope")
        integer(free_bytes, 0, 2**63 - 1)
        if free_bytes < plan.reserve_bytes + plan.allocation:
            raise Blocked(
                "Insufficient space for all capped attempts, captures and reserve"
            )
        cls.private_directory(Path(path).parent)
        # O_EXCL preserves existing evidence even when creation is repeated.
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.close(descriptor)
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE campaign(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    plan TEXT NOT NULL, halt TEXT, started_ns INTEGER NOT NULL,
                    last_seen_ns INTEGER NOT NULL);
                CREATE TABLE attempts(repository TEXT PRIMARY KEY, runner_name TEXT UNIQUE NOT NULL,
                    phase TEXT NOT NULL, disk_bytes INTEGER NOT NULL DEFAULT 0,
                    log_bytes INTEGER NOT NULL DEFAULT 0, deadline_ns INTEGER NOT NULL);
                CREATE TABLE captures(id TEXT PRIMARY KEY, repository TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL);
            """)
            now = time.time_ns()
            db.execute(
                "INSERT INTO campaign VALUES(1,?,NULL,?,?)", (plan.encoded(), now, now)
            )
            db.commit()
        finally:
            db.close()
        return cls(path, plan)

    @staticmethod
    def private_directory(path: Path) -> None:
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise Blocked("Private caller-owned directory required")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise

    def fail(
        self, db: sqlite3.Connection, reason: str, repository: str | None = None
    ) -> None:
        db.execute("UPDATE campaign SET halt=COALESCE(halt,?)", (reason,))
        if repository is not None:
            db.execute(
                "UPDATE attempts SET phase='uncertain' WHERE repository=?",
                (repository,),
            )
        db.commit()  # Preserve failure across the exception and process restart.
        raise Blocked(reason)

    def check(self, db: sqlite3.Connection, free_bytes: int) -> None:
        integer(free_bytes, 0, 2**63 - 1)
        halt = db.execute("SELECT halt FROM campaign").fetchone()[0]
        if halt:
            raise Blocked(halt)
        now = self.check_clock(db)
        expired = db.execute(
            "SELECT repository FROM attempts WHERE phase!='complete' AND deadline_ns<=? LIMIT 1",
            (now,),
        ).fetchone()
        if expired:
            self.fail(db, "Persisted job deadline; stop producers", expired[0])
        uncertain = db.execute(
            "SELECT repository FROM captures WHERE status!='verified' LIMIT 1"
        ).fetchone()
        if uncertain:
            self.fail(
                db, "Unreconciled capture; no further admission or writes", uncertain[0]
            )
        spent = db.execute(
            "SELECT COALESCE(SUM(disk_bytes+log_bytes),0) FROM attempts"
        ).fetchone()[0]
        if free_bytes < self.plan.reserve_bytes + self.plan.allocation - spent:
            self.fail(
                db, "Storage headroom lost; stop producers and preserve originals"
            )

    def check_clock(self, db: sqlite3.Connection) -> int:
        now = self.clock()
        integer(now, 1, 2**63 - 1)
        previous = db.execute("SELECT last_seen_ns FROM campaign").fetchone()[0]
        if now < previous:
            self.fail(db, "Clock moved backwards; deadlines cannot be extended")
        db.execute("UPDATE campaign SET last_seen_ns=?", (now,))
        return now

    def target(self, repository: str) -> Target:
        targets = [t for t in self.plan.targets if t.repository == repository]
        if len(targets) != 1:
            raise Blocked("Unknown repository")
        return targets[0]

    def exact(self, repository: str, observed: dict[str, Any]) -> bool:
        expected = self.target(repository).expected(self.plan.campaign_label)
        return all(
            type(observed.get(k)) is type(v) and observed[k] == v
            for k, v in expected.items()
        )

    def admit(
        self,
        repository: str,
        observed: dict[str, Any],
        free_bytes: int,
        elapsed_seconds: int,
    ) -> str:
        integer(elapsed_seconds, 0, 2**63 - 1)
        with self.transaction() as db:
            self.check(db, free_bytes)
            if (
                not self.exact(repository, observed)
                or observed.get("status") != "queued"
            ):
                self.fail(db, "Unexpected requested run/ref/workflow/job identity")
            if elapsed_seconds >= self.plan.admission_seconds:
                raise Blocked("Admission deadline")
            started = db.execute("SELECT started_ns FROM campaign").fetchone()[0]
            now = self.check_clock(db)
            if now >= started + self.plan.admission_seconds * 10**9:
                self.fail(db, "Persisted admission deadline")
            if (
                db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
                >= self.plan.max_attempts
            ):
                raise Blocked("Durable attempt cap exhausted")
            if db.execute(
                "SELECT 1 FROM attempts WHERE repository=?", (repository,)
            ).fetchone():
                raise Blocked("One attempt per target; retries forbidden")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM attempts WHERE phase!='complete'"
                ).fetchone()[0]
                >= self.plan.concurrency
            ):
                raise Blocked("Concurrency occupied, including uncertain cleanup")
            name = "runspindle-campaign-" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO attempts(repository,runner_name,phase,deadline_ns) VALUES(?,?,'reserved',?)",
                (repository, name, now + self.plan.job_seconds * 10**9),
            )
            return name

    def assignment(self, repository: str, observed: dict[str, Any]) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT runner_name,phase,deadline_ns FROM attempts WHERE repository=?",
                (repository,),
            ).fetchone()
            if not row or row[1] != "reserved":
                self.fail(db, "Assignment without a fresh reservation", repository)
            if (
                not self.exact(repository, observed)
                or observed.get("runner_name") != row[0]
                or observed.get("status") != "in_progress"
            ):
                self.fail(
                    db,
                    "Unexpected actual assignment; attribution is not isolation",
                    repository,
                )
            if db.execute("SELECT halt FROM campaign").fetchone()[0]:
                raise Blocked("Campaign already halted")
            if self.check_clock(db) >= row[2]:
                self.fail(db, "Persisted job deadline", repository)
            db.execute(
                "UPDATE attempts SET phase='running' WHERE repository=?", (repository,)
            )

    def charge(
        self,
        db: sqlite3.Connection,
        repository: str,
        kind: str,
        size: int,
        free_bytes: int,
    ) -> None:
        integer(size, 1, 2**63 - 1)
        self.check(db, free_bytes)
        row = db.execute(
            "SELECT phase,disk_bytes,log_bytes FROM attempts WHERE repository=?",
            (repository,),
        ).fetchone()
        if not row or row[0] not in ("reserved", "running"):
            self.fail(db, "No writable attempt", repository)
        limits = {
            "disk": (row[1], DISK_BYTES),
            "log": (row[2], self.plan.log_bytes_per_attempt),
        }
        if kind not in limits:
            raise ValueError("Unknown accounting class")
        used, limit = limits[kind]
        if used + size > limit:
            self.fail(
                db, "Disk/log budget exhausted; no new write permitted", repository
            )
        db.execute(
            "UPDATE attempts SET "
            + kind
            + "_bytes="
            + kind
            + "_bytes+? WHERE repository=?",
            (size, repository),
        )

    def account_disk(self, repository: str, size: int, free_bytes: int) -> None:
        with self.transaction() as db:
            self.charge(db, repository, "disk", size, free_bytes)

    def capture(self, repository: str, payload: bytes, free_bytes: int) -> Path:
        if type(payload) is not bytes or not 1 <= len(payload) <= 1024**2:
            raise ValueError("Bounded immutable capture chunk required")
        identity, sha = uuid.uuid4().hex, hashlib.sha256(payload).hexdigest()
        path = self.path.parent / ("capture-" + identity + ".log")
        # Charge before the effect. Charges survive partial or unknown writes.
        with self.transaction() as db:
            self.charge(db, repository, "log", len(payload) + 8192, free_bytes)
            db.execute(
                "INSERT INTO captures VALUES(?,?,?,?,'uncertain')",
                (identity, repository, len(payload), sha),
            )
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise Blocked("Capture verification failed")
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            with self.transaction() as db:
                db.execute(
                    "UPDATE captures SET status='verified' WHERE id=?", (identity,)
                )
            return path
        except BaseException:
            with self.transaction() as db:
                self.fail(
                    db,
                    "Capture uncertain; original/capture and charge retained",
                    repository,
                )
            raise  # pragma: no cover -- fail always raises

    def finish(
        self, repository: str, observed: dict[str, Any], cleanup: dict[str, Any]
    ) -> None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT runner_name,phase,deadline_ns FROM attempts WHERE repository=?",
                (repository,),
            ).fetchone()
            if (
                not row
                or row[1] != "running"
                or not self.exact(repository, observed)
                or observed.get("runner_name") != row[0]
                or observed.get("status") != "completed"
                or observed.get("conclusion") != "success"
            ):
                self.fail(
                    db, "Workload completion identity or result uncertain", repository
                )
            expected = {
                "guest_stopped",
                "runner_removed",
                "slot_empty",
                "artifacts_verified",
            }
            if set(cleanup) != expected or any(
                value is not True for value in cleanup.values()
            ):
                self.fail(
                    db, "Cleanup uncertain; capacity and artifacts retained", repository
                )
            if db.execute(
                "SELECT 1 FROM captures WHERE repository=? AND status!='verified'",
                (repository,),
            ).fetchone():
                self.fail(db, "Unverified capture retained", repository)
            if db.execute("SELECT halt FROM campaign").fetchone()[0]:
                raise Blocked("Campaign already halted")
            if self.check_clock(db) >= row[2]:
                self.fail(db, "Persisted job deadline", repository)
            db.execute(
                "UPDATE attempts SET phase='complete' WHERE repository=?", (repository,)
            )

    def report(self) -> dict[str, Any]:
        with self.transaction() as db:
            rows = db.execute(
                "SELECT repository,phase,disk_bytes,log_bytes FROM attempts ORDER BY repository"
            ).fetchall()
            halt = db.execute("SELECT halt FROM campaign").fetchone()[0]
            return {
                "evidence_class": "offline_campaign_guard_state_not_ci_qualification",
                "guard_coverage": "complete"
                if not halt and len(rows) == 8 and all(r[1] == "complete" for r in rows)
                else "blocked",
                "live_execution": "disabled",
                "halt": halt,
                "attempts_consumed": len(rows),
                "max_attempts": self.plan.max_attempts,
                "attempts": rows,
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    plan = Plan.parse(json.loads(args.plan.read_text()))
    # No State/Ledger construction or operational imports, including --execute.
    print(
        json.dumps(
            {
                "plan": "valid",
                "status": "blocked",
                "live_execution": "disabled",
                "reason": UNSUPPORTED,
                "allocation_bytes": plan.allocation,
                "minimum_free_bytes": plan.reserve_bytes + plan.allocation,
            }
        )
    )
    if args.execute:
        execution_gate()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
