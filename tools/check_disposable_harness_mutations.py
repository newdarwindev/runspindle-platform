#!/usr/bin/env python3
"""Require passing controls and assertion failures for synthetic harness mutants."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "manifest_checksum_bypass": (
        "contracts.py",
        "digest(content) == expected[name]",
        "True",
        "ManifestTests.test_corrupted_floating_missing_license_and_toolchain_fail_closed",
    ),
    "accepted_is_persisted": (
        "telemetry.py",
        "matches = sum(canonical(record) == body for record in records)",
        'matches = 1 if state == "accepted" else 0',
        "TelemetryTests.test_http_acceptance_without_readback_is_loss_not_usage",
    ),
    "uncorrelated_readback": (
        "telemetry.py",
        "canonical(record) == body for record in records",
        "True for record in records",
        "TelemetryTests.test_every_correlation_field_is_required_and_unmatched_canaries_block",
    ),
    "duplicate_usage": (
        "telemetry.py",
        "usage += units",
        "usage += units * matches",
        "TelemetryTests.test_restart_replay_duplicates_do_not_double_count_usage",
    ),
    "ignore_envelope_canary": (
        "telemetry.py",
        '"blocked" if canary_paths(records)',
        '"blocked" if False',
        "TelemetryTests.test_every_correlation_field_is_required_and_unmatched_canaries_block",
    ),
    "cancel_skips_rollback": (
        "packaging.py",
        "except BaseException:",
        "except Exception:",
        "PackagingTests.test_failures_cancellation_at_every_install_boundary_restore_prior_state",
    ),
    "allow_unregistered_endpoint": (
        "runtime.py",
        'raise PermissionError("unexpected outbound request denied")',
        'return 200, b"{}"',
        "LifecycleTests.test_egress_and_cross_run_denied_and_no_socket_created",
    ),
}


def check_case(case: tuple[str, str, str, str]) -> bool:
    filename, old, new, name = case
    with tempfile.TemporaryDirectory(prefix="synthetic-harness-mutation-") as temporary:
        root = Path(temporary)
        for directory in ("tools", "tests", "fixtures", "fleet", ".github"):
            shutil.copytree(
                ROOT / directory,
                root / directory,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        path = root / "tools/disposable_harness" / filename
        original = path.read_text()
        if original.count(old) != 1:
            raise ValueError("mutation anchor drift: " + filename)

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    *(["-O"] if sys.flags.optimize else []),
                    "-m",
                    "unittest",
                    "test_disposable_harness." + name,
                    "-q",
                ],
                cwd=root,
                env={
                    **os.environ,
                    "PYTHONPATH": str(root) + os.pathsep + str(root / "tests"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                capture_output=True,
                text=True,
                timeout=30,
            )

        control = run()
        if control.returncode or "skipped" in control.stderr:
            return False
        path.write_text(original.replace(old, new))
        mutant = run()
        return (
            mutant.returncode == 1
            and "FAIL:" in mutant.stderr
            and "ERROR:" not in mutant.stderr
            and "skipped" not in mutant.stderr
        )


def main() -> int:
    results = [
        {"mutation": name, "detected": check_case(case)} for name, case in CASES.items()
    ]
    print(
        json.dumps(
            {"evidence_class": "synthetic_mutation_detection", "results": results},
            sort_keys=True,
        )
    )
    return int(not all(item["detected"] for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
