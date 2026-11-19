#!/usr/bin/env python3
"""Classify reviewed offline retention samples; never touch a fleet or payload."""

import argparse
import json
import re
from pathlib import Path
from typing import Any

MAX_INPUT = 1 << 20
IDENTITY = {
    "repository": r"newdarwindev/runspindle(?:-[a-z0-9-]+)?",
    "head_sha": r"[0-9a-f]{40}",
    "resource_id": r"[0-9a-f]{32}",
}
NUMERIC_IDENTITY = ("run_id", "run_attempt", "job_id")
PROOFS = {
    "github": ("exact_job_success",),
    "teardown": ("guest_exited", "cgroup_empty", "runner_absent"),
    "retention": ("bounded_receipt_readback", "no_hold", "eligible_under_policy"),
}


def valid_integer(n: object, minimum: int = 0) -> bool:
    return type(n) is int and minimum <= n < 1 << 63


def valid_identity(identity: object) -> bool:
    return (
        type(identity) is dict
        and set(identity) == set(IDENTITY) | set(NUMERIC_IDENTITY)
        and all(
            type(identity[k]) is str and re.fullmatch(p, identity[k])
            for k, p in IDENTITY.items()
        )
        and all(valid_integer(identity[k], 1) for k in NUMERIC_IDENTITY)
    )


def classify(sample: object) -> dict[str, Any]:
    """Estimate only explicitly matched reviewed evidence, not actual freed bytes."""
    if type(sample) is not dict or set(sample) != {
        "identity",
        "overlay_allocated_bytes",
        "compact_allocated_bytes",
        "proofs",
    }:
        raise ValueError("invalid sample envelope")
    identity = sample["identity"]
    overlay, compact = (
        sample["overlay_allocated_bytes"],
        sample["compact_allocated_bytes"],
    )
    proofs = sample["proofs"]
    if (
        not valid_identity(identity)
        or not valid_integer(overlay)
        or type(proofs) is not dict
    ):
        raise ValueError("invalid sample identity or sizing")
    if compact is not None and not valid_integer(compact):
        raise ValueError("invalid compact sizing")
    if not set(proofs) <= PROOFS.keys():
        raise ValueError("unknown proof type")
    reasons = []
    for name, fields in PROOFS.items():
        evidence = proofs.get(name)
        if evidence is None:
            reasons.append(f"{name}_missing")
        elif (
            type(evidence) is not dict
            or set(evidence) != {"identity", *fields}
            or not valid_identity(evidence["identity"])
            or evidence["identity"] != identity
            or not all(evidence[f] is True for f in fields)
        ):
            reasons.append(f"{name}_unverified")
    if compact is None:
        reasons.append("compact_size_unmeasured")
    estimate = 0 if reasons else max(0, overlay - compact)
    return {
        "scope": "report_only_caller_attested_inputs",
        "status": "ineligible" if reasons else "eligible_for_review",
        "reasons": reasons,
        "overlay_allocated_bytes": overlay,
        "estimated_reclaimable_bytes": estimate,
        "actual_freed_bytes": 0,
    }


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    args = parser.parse_args()
    try:
        with args.sample.open("rb") as source:
            data = source.read(MAX_INPUT + 1)
        if len(data) > MAX_INPUT:
            raise ValueError("input size exceeded")
        result = classify(json.loads(data, object_pairs_hook=unique))
    except (OSError, ValueError, RecursionError):
        parser.exit(2, "Invalid retention sample; no report emitted.\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
