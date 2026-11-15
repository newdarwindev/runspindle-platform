#!/usr/bin/env python3
"""Explicit Temporal selection within the existing trusted source-check boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import temporal_lane  # noqa: E402

OPT_IN = "RUNSPINDLE_TEMPORAL_PREPARED"
REQUIRED_IDS = tuple(
    "test_temporal_lane.RealLaneTests." + name
    for name in (
        "test_exception_and_client_failure_cleanup_and_egress_denial",
        "test_existing_unrelated_image_cannot_replace_prepared_release",
        "test_readiness_deadline_cleanup",
        "test_real_restart_persistence_and_success_cleanup",
        "test_sigterm_cleanup_in_fresh_process",
    )
)
CANONICAL_COMMAND = [
    "python3",
    "tools/python_tests.py",
    "--temporal",
    "required",
    "--prepared",
    "/opt/runspindle-temporal",
]


def valid_coverage(value: Any) -> bool:
    return isinstance(value, dict) and value == {
        "mode": "required",
        "status": "passed",
        "started": list(REQUIRED_IDS),
        "passed": list(REQUIRED_IDS),
    }


def prepared_input(directory: Path) -> None:
    """Verify local release inputs before any test (or Docker call) can run."""
    metadata = json.loads((directory / "prepared.json").read_text())
    if metadata["pins_sha256"] != temporal_lane.digest(temporal_lane.PINS):
        raise ValueError("prepared pins mismatch")
    binary, _ = temporal_lane.verified_assets(
        directory, metadata["architecture"], temporal_lane.read_pins()
    )
    if not (
        hashlib.sha256(binary).hexdigest()
        == metadata["binary_sha256"]
        == temporal_lane.digest(directory / "temporal")
    ):
        raise ValueError("prepared binary mismatch")
    if not isinstance(metadata["image"], str) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", metadata["image"]
    ):
        raise ValueError("invalid prepared image identity")


def flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    cases = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            cases.extend(flatten(item))
        else:
            cases.append(item)
    return cases


class ObservedResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.started_ids: list[str] = []
        self.passed_ids: list[str] = []

    def startTest(self, test: unittest.TestCase) -> None:
        self.started_ids.append(test.id())
        super().startTest(test)

    def addSuccess(self, test: unittest.TestCase) -> None:
        self.passed_ids.append(test.id())
        super().addSuccess(test)


def run(mode: str, prepared: Path | None) -> tuple[int, dict[str, Any]]:
    # Neither ambient opt-in nor presence of /opt selects integration.
    os.environ.pop(OPT_IN, None)
    if mode not in ("boundary", "required") or (mode == "boundary" and prepared):
        raise ValueError("invalid explicit selection")
    if mode == "required":
        if prepared is None:
            raise ValueError("required prepared input missing")
        prepared_input(prepared)
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    module = sys.modules["test_temporal_lane"]
    real_class = module.RealLaneTests
    cases = flatten(suite)
    real_cases = [test for test in cases if type(test) is real_class]
    if sorted(test.id() for test in real_cases) != list(REQUIRED_IDS):
        raise ValueError("required test inventory mismatch")
    if any(
        test.id() in REQUIRED_IDS and type(test) is not real_class for test in cases
    ):
        raise ValueError("substituted required test")
    setattr(module, "PREPARED", str(prepared) if mode == "required" else None)
    real_class.__unittest_skip__ = mode != "required"
    real_class.__unittest_skip_why__ = "explicit boundary selection; Temporal not run"
    result = cast(
        ObservedResult,
        unittest.TextTestRunner(verbosity=2, resultclass=ObservedResult).run(suite),
    )
    coverage = {
        "mode": mode,
        "status": "not_run" if mode == "boundary" else "passed",
        "started": sorted(x for x in result.started_ids if x in REQUIRED_IDS),
        "passed": sorted(x for x in result.passed_ids if x in REQUIRED_IDS),
    }
    success = result.wasSuccessful() and (
        mode == "boundary" or valid_coverage(coverage)
    )
    if mode == "required" and not success:
        coverage["status"] = "failed"
    return int(not success), coverage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporal", choices=("boundary", "required"), required=True)
    parser.add_argument("--prepared", type=Path)
    args = parser.parse_args(argv)
    try:
        code, coverage = run(args.temporal, args.prepared)
    except (OSError, ValueError, KeyError, temporal_lane.Blocked):
        code, coverage = 2, {"mode": args.temporal, "status": "blocked"}
    coverage["invocation"] = os.environ.get("RUNSPINDLE_TEST_INVOCATION", "")
    print("TEMPORAL_COVERAGE=" + json.dumps(coverage, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
