#!/usr/bin/env python3
"""Prove synthetic lifecycle tests reject broken freshness/identity accounting."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUTATIONS = {
    "stale_capacity": (
        "if type(at) not in (int, float) or not 0 <= now - at < freshness_seconds:",
        "if False:",
    ),
    "recipe_drift": ('if manifest.get("inputs") != inputs:', "if False:"),
    "uncertain_capacity": (
        'occupied = {r["slot"] for r in resources}',
        "occupied = set()",
    ),
}


def main() -> None:
    for name, (before, after) in MUTATIONS.items():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(
                ROOT / "tools/runner_fleet",
                root / "tools/runner_fleet",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            shutil.copy(ROOT / "tools/fleet.py", root / "tools/fleet.py")
            (root / "tests").mkdir()
            shutil.copy(
                ROOT / "tests/test_fleet_lifecycle.py",
                root / "tests/test_fleet_lifecycle.py",
            )
            (root / "fleet").mkdir()
            for path in ("config.json", "image-inputs.json"):
                shutil.copy(ROOT / "fleet" / path, root / "fleet" / path)
            target = root / "tools/runner_fleet/lifecycle.py"
            source = target.read_text()
            if source.count(before) != 1:
                raise SystemExit("Mutation anchor drift: " + name)
            target.write_text(source.replace(before, after))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(root / "tests"),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if (
                result.returncode == 0
                or "FAIL:" not in result.stderr
                or "ERROR:" in result.stderr
            ):
                print(result.stdout + result.stderr)
                raise SystemExit("Mutation survived or test harness failed: " + name)
            print(name + ": rejected by assertion")


if __name__ == "__main__":
    main()
