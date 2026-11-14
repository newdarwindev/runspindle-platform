"""Read-only identity and capacity observations, never fleet qualification."""

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, cast

from .backend import digest


def capacity_snapshot(
    state: Any, guests: Any, service: Any, now: float, freshness_seconds: float = 120
) -> dict[str, Any]:
    """Unknown ownership and stale inventory must not advertise free capacity.

    Observed guest slots and durable reservations are independent inputs. This
    reports availability; it does not alter scheduling or reconcile resources.
    """
    reasons = []
    for key in ("heartbeat", "poll_at"):
        at = state.get(key)
        if type(at) not in (int, float) or not 0 <= now - at < freshness_seconds:
            reasons.append("stale or unavailable " + key)
    if service.get("ActiveState") != "active":
        reasons.append("manager service unavailable")
    if state.get("error"):
        reasons.append("manager inventory error")
    resources = state.active()
    occupied = {r["slot"] for r in resources}
    if len(resources) > 2 or any(
        type(r["slot"]) is not int
        or r["slot"] not in (0, 1)
        or r["phase"]
        not in (
            "reserved",
            "starting",
            "isolated",
            "registering",
            "running",
            "finishing",
            "cleaning",
            "uncertain",
        )
        for r in resources
    ):
        reasons.append("invalid durable resource inventory")
    valid_guests = (
        [
            g
            for g in guests
            if isinstance(g, dict)
            and type(g.get("slot")) is int
            and g["slot"] in (0, 1)
        ]
        if isinstance(guests, list)
        else []
    )
    by_slot = {g["slot"]: g for g in valid_guests}
    if (
        not isinstance(guests, list)
        or len(guests) != 2
        or len(valid_guests) != 2
        or set(by_slot) != {0, 1}
    ):
        reasons.append("incomplete guest observations")
    for slot in range(2):
        guest = by_slot.get(slot, {})
        phase = guest.get("ActiveState")
        if phase not in ("active", "activating", "deactivating", "inactive", "failed"):
            reasons.append("unavailable guest observation")
        try:
            pid = int(guest["MainPID"])
            if pid < 0 or phase in ("inactive", "failed") and pid != 0:
                reasons.append("unreconciled guest process")
        except (KeyError, TypeError, ValueError):
            reasons.append("unavailable guest process observation")
        if phase in ("active", "activating", "deactivating"):
            occupied.add(slot)
            owners = [r for r in resources if r["slot"] == slot]
            if len(owners) != 1 or guest.get("resource_id") != owners[0]["id"]:
                reasons.append("unreconciled guest ownership")
        for resource in (r for r in resources if r["slot"] == slot):
            if resource["phase"] in ("running", "finishing") and phase != "active":
                reasons.append("guest loss awaiting reconciliation")
    drained = state.get("drained", True)
    return {
        "status": "blocked" if reasons else "passed",
        "evidence_class": "capacity_observation",
        "configured_slots": 2,
        "occupied_slots": sorted(occupied),
        "available_slots": max(0, 2 - len(occupied))
        if not reasons and not drained
        else 0,
        "drained": drained,
        "reasons": sorted(set(reasons)),
        "lifecycle_qualification": "not_run",
    }


def image_matrix(
    cfg: dict[str, Any], inputs_path: Path, image: Path, manifest_path: Path
) -> dict[str, Any]:
    """Hash actual local bytes against a manifest and this source's recipe.

    A matching recipe and image identify candidate bytes only. The manifest is
    build output, not an independent observation of installed tools or faults.
    """
    inputs_bytes = inputs_path.read_bytes()
    inputs = json.loads(inputs_bytes)
    reasons = []
    actual = None
    try:
        actual = cast(Callable[[Path], str], digest)(image)
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict):
            raise ValueError("invalid image manifest")
        if manifest.get("sha256") != actual:
            reasons.append("image digest mismatch")
        if manifest.get("inputs") != inputs:
            reasons.append("image recipe mismatch")
    except (OSError, ValueError, TypeError):
        reasons.append("image or manifest unavailable")
    return {
        "schema_version": 1,
        "evidence_class": "local_image_identity",
        "status": "blocked",
        "recipe_sha256": hashlib.sha256(inputs_bytes).hexdigest(),
        "lanes": [
            {
                "lane": name,
                "os": lane["os"],
                "architecture": lane["architecture"],
                "backend": lane["backend"],
                "resources": {
                    "host_shared_capacity": cfg["hosts"][lane["host"]]["capacity"],
                    "cpus_per_guest": lane["cpus"],
                    "memory_gib_per_guest": lane["memory_gib"],
                    "host_reserve_memory_gib": cfg["hosts"][lane["host"]][
                        "reserve_memory_gib"
                    ],
                    "minimum_disk_gib": cfg["hosts"][lane["host"]]["minimum_disk_gib"],
                },
                "image_sha256": actual if lane["host"] == "strix" else None,
                "identity_status": ("blocked" if reasons else "passed")
                if lane["host"] == "strix"
                else "blocked",
                "identity_reasons": reasons
                if lane["host"] == "strix"
                else ["target image and backend qualification unavailable"],
                "tooling": "manifest_only_unverified"
                if lane["host"] == "strix"
                else "not_run",
                "lifecycle_qualification": "not_run",
                "qualification": "blocked",
                "remaining_prerequisites": [
                    "independent installed-tool and offline-dependency observations",
                    "actual cancellation guest-loss restart and cleanup observations",
                    "cross-job isolation and shared-capacity fairness observations",
                ]
                + (
                    []
                    if lane["host"] == "strix"
                    else [
                        "target image recipe and license inventory",
                        "implemented disposable backend and manager",
                        "target host memory disk virtualization and isolation gates",
                    ]
                ),
            }
            for name, lane in cfg["lanes"].items()
        ],
        "product_acceptance": "not_run",
    }
