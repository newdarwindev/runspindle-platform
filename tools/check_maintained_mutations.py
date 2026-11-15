#!/usr/bin/env python3
"""Check mutation sensitivity without confusing portable and native evidence.

The default installed suite retains the Linux arm64 installation requirements.
The explicit portable suite checks synthetic method-level invariants and cannot
replace the mandatory installed gate or qualify any installation target.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "journal_family_fence_disabled": (
        "development_bundle.py",
        "def state(self):\n        self._check_journal_kind()",
        "def state(self):\n        # deliberately bypass journal family fence",
        "test_streaming_bundle.InstalledStreamingTests.test_metadata_and_stream_journals_cannot_cross_payload_kinds",
    ),
    "stream_checksum_ignored": (
        "streaming_bundle.py",
        'check(h.hexdigest() == item["sha256"], "bundle checksum mismatch")',
        'check(True, "bundle checksum mismatch")',
        "test_streaming_bundle.StreamingTests.test_large_file_extract_and_integrity_negative_cases",
    ),
    "stream_installed_modes_ignored": (
        "streaming_bundle.py",
        'check(actual == manifest["files"], "installed byte or mode mismatch")',
        'check(True, "installed byte or mode mismatch")',
        "test_streaming_bundle.InstalledStreamingTests.test_installed_corruption_and_unsafe_links",
    ),
    "fabricated_persistence": (
        "collector_privacy_check.py",
        '"persisted_file_only" if matching else "unknown"',
        '"persisted_file_only" if http_accepted else "unknown"',
        "test_telemetry_evidence.TelemetryEvidenceTests.test_http_acceptance_without_backend_readback_stays_unknown",
    ),
    "wrong_installed_attribution": (
        "development_bundle.py",
        '"manifest_sha256": digest,\n        "files": len(files),',
        '"manifest_sha256": "0" * 64,\n        "files": len(files),',
        "test_development_bundle.InstalledTests.test_real_installed_bytes_and_empty_history_concurrent_generation",
    ),
}
PORTABLE_TESTS = {
    "journal_family_fence_disabled": "test_portable_install_invariants.PortableInstallInvariants.test_journal_family_rejection",
    "stream_installed_modes_ignored": "test_portable_install_invariants.PortableInstallInvariants.test_tree_corruption_and_modes",
    "wrong_installed_attribution": "test_portable_install_invariants.PortableInstallInvariants.test_attribution_readback",
}


def run_test(root: Path, test: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(root) + os.pathsep + str(root / "tests"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [
            sys.executable,
            *(["-" + "O" * sys.flags.optimize] if sys.flags.optimize else []),
            "-m",
            "unittest",
            test,
            "-q",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def mutation_detected(result: subprocess.CompletedProcess[str]) -> bool:
    return (
        result.returncode == 1
        and "FAIL:" in result.stderr
        and "ERROR:" not in result.stderr
        and "skipped" not in result.stderr
    )


def run_case(name: str, case: tuple[str, str, str, str], suite: str) -> bool:
    filename, old, new, test = case
    if suite == "portable":
        test = PORTABLE_TESTS.get(name, test)
    with tempfile.TemporaryDirectory(prefix="platform-evidence-mutation-") as temporary:
        root = Path(temporary)
        for folder in ("tools", "tests"):
            shutil.copytree(
                ROOT / folder,
                root / folder,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        path = root / "tools" / filename
        source = path.read_text()
        if source.count(old) != 1:
            raise ValueError("mutation anchor drift: " + name)
        # A broken or skipped control cannot establish sensitivity to a mutant.
        control = run_test(root, test)
        if control.returncode != 0 or "skipped" in control.stderr:
            print(control.stderr, file=sys.stderr)
            return False
        path.write_text(source.replace(old, new))
        result = run_test(root, test)
        detected = mutation_detected(result)
        if not detected:
            print(result.stderr, file=sys.stderr)
        return detected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite", choices=("installed", "portable"), default="installed"
    )
    args = parser.parse_args(argv)
    results = [
        {"mutation": name, "detected": run_case(name, case, args.suite)}
        for name, case in CASES.items()
    ]
    if args.suite == "portable":
        print(
            json.dumps(
                {
                    "evidence_class": "portable_component_mutation_detection",
                    "native_acceptance": "not_run",
                    "results": results,
                }
            )
        )
    else:
        # Preserve the existing default report and installed test selection.
        print(
            json.dumps(
                {"evidence_class": "component_mutation_detection", "results": results}
            )
        )
    return 0 if all(item["detected"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
