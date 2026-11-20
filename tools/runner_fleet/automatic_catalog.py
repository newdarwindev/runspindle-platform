"""Strict trusted-operator catalog; no implicit qualification or activation.

Only owner-installed, root-owned read-only catalog/manifests/evidence are accepted.
This is local integrity admission, not cryptographic proof of a test or approval.
The installer must independently review evidence and eligible workflow authority.
No current repository is automatically catalogued by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .automatic_policy import ImageRoute

CATALOG = Path("/opt/runspindle-fleet/fleet/automatic-catalog.json")


def sealed_bytes(path: Path, maximum: int) -> bytes:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Noncanonical catalog artifact")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_nlink != 1
            or before.st_mode & 0o222
            or before.st_size > maximum
        ):
            raise ValueError("Unsealed catalog artifact")
        payload = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_mode",
        "st_uid",
        "st_nlink",
    )
    if len(payload) > maximum or any(
        getattr(before, key) != getattr(after, key) for key in fields
    ):
        raise ValueError("Catalog artifact changed")
    return payload


def digest(value: object) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Exact digest required")
    return value


def committed(value: object) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Exact source revision required")
    return value


@dataclass(frozen=True)
class QualifiedRecord:
    route: ImageRoute
    source_sha: str
    workflow_sha256: str
    image_path: Path
    manifest_path: Path
    manifest_sha256: str
    evidence_path: Path
    evidence_sha256: str
    required_checks: tuple[str, ...]

    @classmethod
    def parse(cls, row: dict[str, Any]) -> QualifiedRecord:
        if type(row) is not dict or set(row) != {
            "repository",
            "suite",
            "source_sha",
            "workflow_sha256",
            "image_sha256",
            "tool_closure_sha256",
            "validation_manifest_sha256",
            "image_path",
            "manifest_path",
            "manifest_sha256",
            "evidence_path",
            "evidence_sha256",
            "required_checks",
        }:
            raise ValueError("Unknown or missing qualification fields")
        route = ImageRoute(
            row["repository"],
            digest(row["image_sha256"]),
            digest(row["tool_closure_sha256"]),
            digest(row["validation_manifest_sha256"]),
            row["suite"],
        )
        route.labels()
        checks = row["required_checks"]
        if (
            type(checks) is not list
            or not 1 <= len(checks) <= 256
            or any(
                type(x) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,95}", x)
                for x in checks
            )
            or len(set(checks)) != len(checks)
        ):
            raise ValueError("Exact nonempty check inventory required")
        required = (
            {
                "quality",
                "component",
                "rust",
                "mutation_1",
                "mutation_2",
                "mutation_3",
                "mutation_4",
                "aggregate",
            }
            if route.suite == "validation"
            else {"normal", "optimized", "sandbox", "cleanup"}
        )
        if set(checks) != required:
            raise ValueError("Partial workflow is not suite qualification")
        paths = []
        for name in ("image_path", "manifest_path", "evidence_path"):
            value = row[name]
            if (
                type(value) is not str
                or len(value) > 4096
                or not Path(value).is_absolute()
                or ".." in Path(value).parts
            ):
                raise ValueError("Exact absolute artifact path required")
            paths.append(Path(value))
        return cls(
            route,
            committed(row["source_sha"]),
            digest(row["workflow_sha256"]),
            paths[0],
            paths[1],
            digest(row["manifest_sha256"]),
            paths[2],
            digest(row["evidence_sha256"]),
            tuple(checks),
        )

    def labels(self) -> list[str]:
        """Exact-source route until broader closure applicability is reviewed."""
        return [self.route.labels()[0] + "-source-" + self.source_sha]

    def verify(self) -> None:
        manifest_bytes = sealed_bytes(self.manifest_path, 1024 * 1024)
        evidence_bytes = sealed_bytes(self.evidence_path, 1024 * 1024)
        if (
            hashlib.sha256(manifest_bytes).hexdigest() != self.manifest_sha256
            or hashlib.sha256(evidence_bytes).hexdigest() != self.evidence_sha256
        ):
            raise ValueError("Catalog evidence bytes differ")
        manifest = json.loads(manifest_bytes)
        evidence = json.loads(evidence_bytes)
        if (
            type(manifest) is not dict
            or manifest.get("sha256") != self.route.image_sha256
            or manifest.get("candidate_path") != str(self.image_path)
            or manifest.get("tool_closure_sha256") != self.route.tool_closure_sha256
        ):
            raise ValueError("Image manifest differs")
        expected = {
            "schema": "qualified-workflow-v1",
            "repository": self.route.repository,
            "suite": self.route.suite,
            "source_sha": self.source_sha,
            "workflow_sha256": self.workflow_sha256,
            "image_sha256": self.route.image_sha256,
            "tool_closure_sha256": self.route.tool_closure_sha256,
            "validation_manifest_sha256": self.route.validation_manifest_sha256,
            "status": "passed",
            "isolation": "passed",
            "cleanup": "verified",
            "checks": {name: "passed" for name in self.required_checks},
        }
        if evidence != expected:
            raise ValueError("Qualification is incomplete or differently scoped")
        # Image hash and installed recipe/controller verification stay mandatory
        # in Backend.create. This catalog does not bypass that existing gate.


def load_catalog(path: Path = CATALOG) -> tuple[QualifiedRecord, ...]:
    value = json.loads(sealed_bytes(path, 1024 * 1024))
    if (
        type(value) is not dict
        or set(value) != {"schema", "activation_allowed", "records"}
        or value["schema"] != "automatic-catalog-v1"
    ):
        raise ValueError("Unknown automatic catalog")
    if value["activation_allowed"] is not True:
        raise ValueError("Automatic activation disabled")
    rows = value["records"]
    if type(rows) is not list or not 1 <= len(rows) <= 16:
        raise ValueError("Missing or oversized qualification catalog")
    records = tuple(QualifiedRecord.parse(row) for row in rows)
    if len({(r.route.repository, r.route.suite) for r in records}) != len(records):
        raise ValueError("Ambiguous repository suite qualification")
    for record in records:
        record.verify()
    return records
