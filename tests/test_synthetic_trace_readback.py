"""Rehearse fixture dependency and trace readback gates without any services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest


SCRIPT = Path(__file__).resolve()
FIXTURE = SCRIPT.with_name("fixtures") / "synthetic-trace-readback.json"
CASES = {
    "verified": "verified_fixture",
    "wrong-digest": "dependency_digest_mismatch",
    "missing-notice": "dependency_notice_missing",
    "ack-only": "trace_unavailable",
    "wrong-trace": "trace_mismatch",
}


def check_dependency(expected: dict[str, str], received: dict[str, str]) -> str | None:
    if received.get("image") != expected["image"]:
        return "wrong_dependency"
    if received.get("digest") != expected["digest"]:
        return "dependency_digest_mismatch"
    if not received.get("notice"):
        return "dependency_notice_missing"
    if received["notice"] != expected["notice"]:
        return "dependency_notice_mismatch"
    return None


def reopen_trace(path: Path, expected: dict[str, Any]) -> str:
    try:
        with path.open("r", encoding="utf-8") as stream:
            observed = json.load(stream)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return "trace_unavailable"
    return "verified_fixture" if observed == [expected] else "trace_mismatch"


def rehearse(scenario: str) -> dict[str, Any]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    observed_dependency = dict(fixture["dependency"])
    trace = dict(fixture["trace"])
    if scenario == "wrong-digest":
        observed_dependency["digest"] = "sha256:" + "b" * 64
    elif scenario == "missing-notice":
        del observed_dependency["notice"]
    outcome = check_dependency(fixture["expected_dependency"], observed_dependency)
    write_attempts = 0
    persisted = False
    with tempfile.TemporaryDirectory(prefix="synthetic-trace-") as temporary:
        root = Path(temporary)
        storage = root / "sink.json"
        if outcome is None:
            write_attempts += 1
            # Simulated acceptance and persisted bytes are deliberately separate.
            if scenario != "ack-only":
                row = {**trace, "id": "fixture-unrelated"} if scenario == "wrong-trace" else trace
                storage.write_text(json.dumps([row]), encoding="utf-8")
            outcome = reopen_trace(storage, trace)
        persisted = storage.exists()
    cleared = not root.exists()
    return {
        "synthetic_only": True,
        "live_langfuse_verified": False,
        "scenario": scenario,
        "outcome": outcome,
        "write_attempts": write_attempts,
        "fixture_write_acknowledged": bool(write_attempts),
        "fixture_file_present": persisted,
        "independent_readback_verified": outcome == "verified_fixture",
        "teardown_cleared": cleared,
        "passed": (
            outcome == CASES[scenario]
            and cleared
            and write_attempts == (0 if scenario in {"wrong-digest", "missing-notice"} else 1)
            and persisted == (scenario in {"verified", "wrong-trace"})
        ),
    }


class SyntheticTraceReadbackTests(unittest.TestCase):
    def test_cli_repeats_each_case_and_distinguishes_ack_from_readback(self) -> None:
        for case, outcome in CASES.items():
            with self.subTest(case=case):
                cmd = [sys.executable, str(SCRIPT), case]
                output = subprocess.check_output(cmd, text=True)
                self.assertEqual(output, subprocess.check_output(cmd, text=True))
                receipt = json.loads(output)
                self.assertTrue(receipt["passed"])
                self.assertTrue(receipt["synthetic_only"])
                self.assertFalse(receipt["live_langfuse_verified"])
                self.assertEqual(receipt["outcome"], outcome)
                self.assertEqual(receipt["independent_readback_verified"], case == "verified")
                self.assertTrue(receipt["teardown_cleared"])

    def test_exact_trace_id_does_not_cover_wrong_payload_or_duplicate(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        trace = fixture["trace"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trace.json"
            for observations in ([{**trace, "payload": {}}], [trace, trace]):
                with self.subTest(observations=observations):
                    path.write_text(json.dumps(observations), encoding="utf-8")
                    self.assertEqual(reopen_trace(path, trace), "trace_mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=CASES)
    receipt = rehearse(parser.parse_args().scenario)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
