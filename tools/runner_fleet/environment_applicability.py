"""Offline compatibility policy primitive, NOT admission or a passing verdict.

Inputs must come from a separately trusted complete exact-commit Git observer.
This module does not read Git, authenticate observations, qualify images, audit
GitHub authority, register runners or change the existing v1 catalog. Profiles
are reviewed operator inputs, never job-controlled declarations. Application
classification is not a security boundary. No profile is installed by default.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

MAX_FILES = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TREE_BYTES = 128 * 1024 * 1024


def _hex(value: str, width: int) -> str:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{%d}" % width, value):
        raise ValueError("Invalid digest or revision")
    return value


def _path(value: str) -> str:
    if (
        type(value) is not str
        or len(value) > 512
        or not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
        or any(ord(character) < 32 or ord(character) > 126 for character in value)
    ):
        raise ValueError("Invalid repository path")
    return value


@dataclass(frozen=True)
class Blob:
    path: str
    mode: str
    size: int
    sha256: str

    def validate(self) -> None:
        _path(self.path)
        _hex(self.sha256, 64)
        if self.mode not in {"100644", "100755"}:
            raise ValueError("Only regular Git blobs are supported")
        if type(self.size) is not int or not 0 <= self.size <= MAX_FILE_BYTES:
            raise ValueError("Blob size out of bounds")


def _blobs(values: tuple[Blob, ...]) -> dict[str, Blob]:
    if type(values) is not tuple or len(values) > MAX_FILES:
        raise ValueError("Bounded immutable blob inventory required")
    result: dict[str, Blob] = {}
    total = 0
    for value in values:
        if type(value) is not Blob:
            raise ValueError("Invalid blob")
        value.validate()
        if value.path in result:
            raise ValueError("Duplicate blob")
        result[value.path] = value
        total += value.size
        if total > MAX_TREE_BYTES:
            raise ValueError("Tree size out of bounds")
    for path in result:
        parts = path.split("/")
        if any("/".join(parts[:index]) in result for index in range(1, len(parts))):
            raise ValueError("File/directory collision")
    return result


def _paths(values: tuple[str, ...]) -> set[str]:
    if type(values) is not tuple or len(values) > MAX_FILES:
        raise ValueError("Bounded immutable paths required")
    result = {_path(value) for value in values}
    if len(result) != len(values):
        raise ValueError("Duplicate paths")
    return result


@dataclass(frozen=True)
class Profile:
    """Closed reviewed setup closure; adding even application files needs review.

    External closure digest commits to separately resolved immutable action and
    container/transitive inputs. This primitive cannot verify that resolution.
    Setup includes every other tracked file, so undeclared additions fail closed.
    """

    repository: str
    environment_sha256: str
    external_closure_sha256: str
    setup: tuple[Blob, ...]
    application_paths: tuple[str, ...]
    absent_paths: tuple[str, ...]

    def fingerprint(self) -> str:
        if (
            type(self.repository) is not str
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
                self.repository,
            )
            or len(self.repository) > 256
        ):
            raise ValueError("Exact repository required")
        _hex(self.environment_sha256, 64)
        _hex(self.external_closure_sha256, 64)
        setup = _blobs(self.setup)
        application = _paths(self.application_paths)
        absent = _paths(self.absent_paths)
        if not setup or set(setup) & application or (set(setup) | application) & absent:
            raise ValueError("Empty or overlapping profile")
        all_paths = set(setup) | application | absent
        if len(all_paths) > MAX_FILES:
            raise ValueError("Profile size out of bounds")
        for path in all_paths:
            if any(other.startswith(path + "/") for other in all_paths):
                raise ValueError("Overlapping path ancestors")
        payload = {
            "schema": "environment-compatibility-v2-prototype",
            "repository": self.repository,
            "environment": self.environment_sha256,
            "external": self.external_closure_sha256,
            "setup": [
                [b.path, b.mode, b.size, b.sha256]
                for b in sorted(self.setup, key=lambda b: b.path)
            ],
            "application": sorted(application),
            "absent": sorted(absent),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Observation:
    repository: str
    source_sha: str
    environment_sha256: str
    external_closure_sha256: str
    blobs: tuple[Blob, ...]


def compatible(profile: Profile, observation: Observation) -> str:
    """Return profile fingerprint or reject. A return value is NOT test success."""
    fingerprint = profile.fingerprint()
    _hex(observation.source_sha, 40)
    if observation.repository != profile.repository:
        raise ValueError("Repository mismatch")
    if (
        observation.environment_sha256 != profile.environment_sha256
        or observation.external_closure_sha256 != profile.external_closure_sha256
    ):
        raise ValueError("Environment or external closure mismatch")
    current = _blobs(observation.blobs)
    setup = _blobs(profile.setup)
    if set(current) != set(setup) | set(profile.application_paths):
        raise ValueError("Missing or unclassified path")
    if any(current[path] != blob for path, blob in setup.items()):
        raise ValueError("Setup closure changed")
    # Application blobs may change content but cannot become executable setup.
    if any(current[path].mode != "100644" for path in profile.application_paths):
        raise ValueError("Application mode unsupported")
    return fingerprint


def assigned_compatible(
    profile: Profile, requested: Observation, assigned: Observation
) -> str:
    """Require independently observed exact assignment; no scheduler guarantee.

    Run/job/attempt/runner binding and authority remain coordinator prerequisites.
    Even a different but compatible source is not the requested qualification.
    """
    expected = compatible(profile, requested)
    actual = compatible(profile, assigned)
    if (
        assigned.repository != requested.repository
        or assigned.source_sha != requested.source_sha
        or assigned.blobs != requested.blobs
        or actual != expected
    ):
        raise ValueError("Unexpected actual assignment")
    return actual
