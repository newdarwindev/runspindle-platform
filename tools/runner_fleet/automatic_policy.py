"""Pure routing/capacity checks for a not-yet-enabled automatic fleet.

These functions consume trusted, independently verified deployment records. They
cannot authenticate qualification evidence or authorize activation. No installed
configuration or current manager calls these functions yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .model import REPOSITORIES

GIB = 1024**3


@dataclass(frozen=True)
class ImageRoute:
    repository: str
    image_sha256: str
    tool_closure_sha256: str
    validation_manifest_sha256: str
    suite: str

    def labels(self) -> list[str]:
        if self.repository not in REPOSITORIES:
            raise ValueError("Repository outside non-demo automatic fleet")
        digests = (
            self.image_sha256,
            self.tool_closure_sha256,
            self.validation_manifest_sha256,
        )
        if any(
            type(v) is not str or not re.fullmatch(r"[0-9a-f]{64}", v) for v in digests
        ):
            raise ValueError("Missing exact qualified image bindings")
        if self.suite not in ("validation", "browser-account-e2e"):
            raise ValueError("Unreviewed suite route")
        if (
            self.suite == "browser-account-e2e"
            and self.repository != "runspindle-browser"
        ):
            raise ValueError("Browser suite on wrong repository")
        identity = [
            "automatic-route-v1",
            self.repository,
            "linux",
            "x64",
            *digests,
            self.suite,
        ]
        key = hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()
        # No generic labels: GitHub uses subset matching. Adding self-hosted/linux/
        # x64 would make old generic placeholder jobs eligible again.
        return ["runspindle-linux-x64-" + key]

    def matches(self, repository: str, requested: object) -> bool:
        labels = self.labels()
        return (
            repository == self.repository
            and type(requested) is list
            and requested == labels
        )


def capacity_one(
    *,
    occupied: int,
    available_memory: int,
    filesystem_free: int,
    pool_free: int,
    pool_metadata_basis_points: int,
    retention_allowance: int,
) -> int:
    """Admission allowances, not hard quotas. Caller holds canonical fleet lock.

    filesystem_free is the minimum across independently verified actual state,
    slot and retention filesystems. pool_free must be their verified backing
    pool, not an unrelated spacious mount. Retention allowance is the remaining
    owner-approved budget. Unknown reservations count as occupied. No deletion
    or future compression is counted as capacity.
    """
    values = (
        occupied,
        available_memory,
        filesystem_free,
        pool_free,
        pool_metadata_basis_points,
        retention_allowance,
    )
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("Invalid capacity observation")
    if occupied or pool_metadata_basis_points >= 8000:
        return 0
    # One24GiB logical overlay plus1GiB bounded metadata allowance,30GiB reserve.
    # A retained-base reference must prevent base GC independently.
    admission = 25 * GIB
    if (
        available_memory < 13 * GIB
        or min(filesystem_free, pool_free) < admission + 30 * GIB
        or retention_allowance < admission
    ):
        return 0
    return 1
