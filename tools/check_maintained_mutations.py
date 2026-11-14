#!/usr/bin/env python3
"""Assert real evidence checks reject fabricated persistence/installed attribution."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

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
        '"manifest_sha256": digest,\n            "files": len(files),',
        '"manifest_sha256": "0" * 64,\n            "files": len(files),',
        "test_development_bundle.InstalledTests.test_real_installed_bytes_and_empty_history_concurrent_generation",
    ),
}
results = []
for name, (filename, old, new, test) in CASES.items():
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
        assert source.count(old) == 1, name
        path.write_text(source.replace(old, new))
        env = {
            **os.environ,
            "PYTHONPATH": str(root) + os.pathsep + str(root / "tests"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, "-m", "unittest", test, "-q"],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        detected = (
            result.returncode == 1
            and "FAIL:" in result.stderr
            and "ERROR:" not in result.stderr
            and "skipped" not in result.stderr
        )
        results.append({"mutation": name, "detected": detected})
        if not detected:
            print(result.stderr, file=sys.stderr)
print(
    json.dumps({"evidence_class": "component_mutation_detection", "results": results})
)
raise SystemExit(0 if all(r["detected"] for r in results) else 1)
