#!/usr/bin/env python3
"""Run issue #1's stdlib-only synthetic lane with bounded, attributable evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import signal
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.check_disposable_harness_mutations import CASES, check_case  # noqa: E402
from tools.disposable_harness.contracts import (  # noqa: E402
    DEPENDENCIES,
    integration_plan,
    load_manifest,
    toolchain_status,
)


class Observed(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.scenarios: list[dict[str, str]] = []
        self.subcases: dict[str, int] = {}

    def addSubTest(
        self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any
    ) -> None:
        super().addSubTest(test, subtest, err)
        self.subcases[test.id()] = self.subcases.get(test.id(), 0) + 1
        self.scenarios.append(
            {
                "scenario_id": test.id() + "#case-" + str(self.subcases[test.id()]),
                "status": "passed" if err is None else "failed",
                "assertions": "unittest subcase assertions",
            }
        )

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self.scenarios.append(
            {
                "scenario_id": test.id(),
                "status": "passed",
                "assertions": "unittest assertions completed",
            }
        )

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self.scenarios.append(
            {"scenario_id": test.id(), "status": "not_run", "reason": reason}
        )


def source() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), *args], text=True, timeout=10
        ).strip()

    names = [
        "tools/check_disposable_harness.py",
        "tools/check_disposable_harness_mutations.py",
        "tests/test_disposable_harness.py",
        "tools/collector_privacy_check.py",
        "tools/runner_fleet/model.py",
        "fleet/config.json",
    ]
    names += [
        str(p.relative_to(ROOT))
        for folder in (
            "tools/disposable_harness",
            "fixtures/harness",
            ".github/workflows",
        )
        for p in (ROOT / folder).glob("*")
        if p.is_file()
    ]
    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "input_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in sorted(names)
        },
    }


def no_network(event: str, args: tuple[Any, ...]) -> None:
    if event.startswith("socket."):
        raise PermissionError("synthetic harness forbids socket operations")


def execute(output: Path) -> int:
    start = datetime.now(timezone.utc).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {"status": "not_run", "reason": "incomplete_run", "started_at": start}
        )
        + "\n"
    )
    before = source()
    load_manifest(ROOT / "fixtures/harness")
    runtime = toolchain_status()
    if runtime["status"] != "passed":
        output.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "toolchain": runtime,
                    "tests": "not_run",
                    "source": before,
                },
                indent=2,
            )
            + "\n"
        )
        print("synthetic harness: blocked by unqualified interpreter")
        return 2
    sys.addaudithook(no_network)
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_disposable_harness.py"
    )
    runner = unittest.TextTestRunner(verbosity=2, resultclass=Observed)
    result = runner.run(suite)
    mutations = [
        {"mutation": name, "detected": check_case(case)} for name, case in CASES.items()
    ]
    after = source()
    passed = (
        result.wasSuccessful()
        and not result.skipped
        and before == after
        and result.testsRun >= 19
        and all(item["detected"] for item in mutations)
    )
    receipt: dict[str, Any] = {
        "schema": "runspindle.synthetic-harness-evidence.v1",
        "status": "passed" if passed else "failed",
        "evidence_class": "synthetic_with_disposable_local_sqlite_and_signal_probe",
        "source": before,
        "source_stable": before == after,
        "started_at": start,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "seed": 17,
        "clock": "injected deterministic ticks; wall clock only for signal probe deadline",
        "python": platform.python_version(),
        "toolchain": runtime,
        "host": platform.system() + "/" + platform.machine(),
        "tests_run": result.testsRun,
        "tests_skipped": len(result.skipped),
        "mutations": mutations,
        "scenarios": getattr(result, "scenarios", []),
        "failed_scenarios": sorted(
            {test.id() for test, _ in result.errors + result.failures}
        ),
        "optional_integration": [
            integration_plan(name) for name in sorted(DEPENDENCIES)
        ],
        "integration_prerequisite_gates": [
            integration_plan(name, opt_in=True) for name in sorted(DEPENDENCIES)
        ],
        "native_installation": "not_run",
        "installed_service_persistence": "not_run",
        "real_namespace_isolation": "not_run",
        "upstream_schema_url_masking": "blocked_pending_separate_exact_configuration_qualification",
        "independent_review": "pending",
        "live_acceptance": "not_run",
    }
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print("synthetic harness: " + receipt["status"])
    return int(not passed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".verification/disposable-harness.json"
    )
    args = parser.parse_args()
    try:
        return execute(args.output)
    except (
        KeyboardInterrupt,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        # Replace any previous success before qualification, including cancellation.
        receipt = {
            "status": "failed",
            "reason": "cancelled"
            if isinstance(error, KeyboardInterrupt)
            else "validation_failed",
            "error_class": type(error).__name__,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt) + "\n")
        print("synthetic harness: failed")
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":

    def cancelled(*_: Any) -> None:
        raise KeyboardInterrupt("synthetic harness cancelled")

    signal.signal(signal.SIGTERM, cancelled)
    raise SystemExit(main())
