#!/usr/bin/env python3
"""Prove the boundary suite detects deliberate unsafe lane mutations."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
source = (ROOT / "tools/temporal_lane.py").read_text()
mutations = {
    "floating_image": (
        'if not re.fullmatch("sha256:[a-f0-9]{64}", image):',
        "if False:",
    ),
    "missing_volume_cleanup": (
        '("volume", "rm", "--force", self.volume)',
        '("volume", "inspect", self.volume)',
    ),
}
results = []
for name, (old, new) in mutations.items():
    if old not in source:
        raise SystemExit("mutation anchor missing: " + name)
    with tempfile.TemporaryDirectory(
        prefix="synthetic-temporal-mutation-"
    ) as directory:
        root = pathlib.Path(directory)
        for relative in ("tests/test_temporal_lane.py", "temporal/dependencies.json"):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        (root / "tools").mkdir()
        (root / "tools/temporal_lane.py").write_text(source.replace(old, new))
        env = dict(os.environ)
        env.pop("RUNSPINDLE_TEMPORAL_PREPARED", None)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 or "FAIL" not in result.stderr:
            raise SystemExit("unsafe mutation not detected: " + name)
        results.append({"mutation": name, "detected": True})
print(json.dumps({"evidence_class": "synthetic_fault_injection", "mutations": results}))
