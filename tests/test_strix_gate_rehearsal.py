"""Offline classification of invented validation receipts; no runner API."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast


SCRIPT = Path(__file__).resolve()
FIXTURE = SCRIPT.with_name("fixtures") / "strix-gate-rehearsal.json"
CLEANUP = ("slot", "process", "registration")
OUTCOMES = {
    "passed": "verified_fixture",
    "queued-unassigned": "queued_unassigned",
    "wrong-label": "wrong_runner_label",
    "stale-revision": "stale_revision",
    "incomplete-cleanup": "incomplete_cleanup",
    "missing-check": "required_check_not_passed",
}


def _record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("expected a string-keyed fixture object")
    return cast(dict[str, object], value)


def classify(expected: dict[str, object], seen: dict[str, object]) -> str:
    if seen.get("revision") != expected["revision"]:
        return "stale_revision"
    runner = seen.get("runner_id")
    if not isinstance(runner, str) or not runner:
        return "queued_unassigned" if seen.get("job_status") == "queued" else "unassigned"
    if seen.get("runner_label") != expected["runner_label"]:
        return "wrong_runner_label"
    if seen.get("job_status") != "completed":
        return "job_not_complete"

    required = expected["checks"]
    if not isinstance(required, list) or not required or not all(
        isinstance(name, str) for name in required
    ):
        raise ValueError("fixture must name required checks")
    checks = _record(seen.get("checks", {}))
    if any(checks.get(name) != "success" for name in required):
        return "required_check_not_passed"
    cleanup = _record(seen.get("cleanup", {}))
    if any(cleanup.get(name) is not True for name in CLEANUP):
        return "incomplete_cleanup"
    return "verified_fixture"


def replay(scenario: str) -> dict[str, object]:
    fixture = _record(json.loads(FIXTURE.read_text(encoding="utf-8")))
    expected = _record(fixture["expected"])
    observed = _record(fixture["good"]) | _record(_record(fixture["cases"])[scenario])
    outcome = classify(expected, observed)
    return {
        "synthetic_only": True,
        "live_ci_verified": False,
        "scenario": scenario,
        "outcome": outcome,
        "fixture_gate_verified": outcome == "verified_fixture",
        "passed": outcome == OUTCOMES[scenario],
    }


class StrixGateRehearsalTests(unittest.TestCase):
    def test_all_cases_via_cli_have_stable_receipts(self) -> None:
        for scenario, expected in OUTCOMES.items():
            with self.subTest(scenario=scenario):
                cmd = [sys.executable, str(SCRIPT), scenario]
                output = subprocess.check_output(cmd, text=True)
                self.assertEqual(output, subprocess.check_output(cmd, text=True))
                self.assertEqual(
                    json.loads(output),
                    {
                        "synthetic_only": True,
                        "live_ci_verified": False,
                        "scenario": scenario,
                        "outcome": expected,
                        "fixture_gate_verified": scenario == "passed",
                        "passed": True,
                    },
                )

    def test_truthy_cleanup_cannot_substitute_for_confirmed_cleanup(self) -> None:
        fixture = _record(json.loads(FIXTURE.read_text(encoding="utf-8")))
        expected = _record(fixture["expected"])
        good = _record(fixture["good"])
        observed = good | {"cleanup": {"slot": True, "process": True, "registration": 1}}
        self.assertEqual(classify(expected, observed), "incomplete_cleanup")

    def test_queued_assignment_does_not_credit_passed_checks(self) -> None:
        fixture = _record(json.loads(FIXTURE.read_text(encoding="utf-8")))
        expected = _record(fixture["expected"])
        good = _record(fixture["good"])
        self.assertEqual(classify(expected, good | {"job_status": "queued"}), "job_not_complete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=OUTCOMES)
    receipt = replay(parser.parse_args().scenario)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
