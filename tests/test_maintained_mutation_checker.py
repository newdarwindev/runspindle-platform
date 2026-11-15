"""Checker controls are synthetic; these tests never run a native installer."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import check_maintained_mutations as checker


def completed(code: int = 0, stderr: str = "OK") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


class MutationCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "tools/example.py").write_text("old")
        self.case = ("example.py", "old", "new", "synthetic_test")

    def test_requires_genuine_failure_without_errors_or_skips(self) -> None:
        self.assertTrue(checker.mutation_detected(completed(1, "FAIL: test")))
        for result in (
            completed(0, "OK"),
            completed(2, "FAIL: test"),
            completed(1, "ERROR: test"),
            completed(1, "FAIL: test\nERROR: other"),
            completed(1, "FAIL: test\nskipped=1"),
            completed(1, "unrecognized failure"),
        ):
            with self.subTest(result=result):
                self.assertFalse(checker.mutation_detected(result))

    def test_skipped_or_broken_controls_never_count_as_detection(self) -> None:
        for result in (completed(0, "OK (skipped=1)"), completed(1, "FAIL: test")):
            with (
                self.subTest(result=result),
                patch.object(checker, "ROOT", self.root),
                patch.object(checker, "run_test", return_value=result) as run,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertFalse(checker.run_case("example", self.case, "portable"))
                self.assertEqual(run.call_count, 1)

    def test_control_passes_before_mutation_and_original_is_unchanged(self) -> None:
        seen = []

        def run(root: Path, test: str) -> subprocess.CompletedProcess[str]:
            seen.append(((root / "tools/example.py").read_text(), test))
            return completed() if len(seen) == 1 else completed(1, "FAIL: test")

        with (
            patch.object(checker, "ROOT", self.root),
            patch.object(checker, "run_test", side_effect=run),
        ):
            self.assertTrue(checker.run_case("example", self.case, "portable"))
        self.assertEqual(seen, [("old", "synthetic_test"), ("new", "synthetic_test")])
        self.assertEqual((self.root / "tools/example.py").read_text(), "old")

    def test_missing_or_ambiguous_anchor_fails_even_when_optimized(self) -> None:
        for source in ("missing", "old old"):
            (self.root / "tools/example.py").write_text(source)
            with (
                self.subTest(source=source),
                patch.object(checker, "ROOT", self.root),
                patch.object(checker, "run_test") as run,
            ):
                with self.assertRaisesRegex(ValueError, "mutation anchor drift"):
                    checker.run_case("example", self.case, "portable")
                run.assert_not_called()

    def test_explicit_portable_selection_does_not_change_native_selection(self) -> None:
        for name, portable in checker.PORTABLE_TESTS.items():
            native = checker.CASES[name][3]
            case = (*self.case[:3], native)
            for suite, expected in (("installed", native), ("portable", portable)):
                with (
                    self.subTest(name=name, suite=suite),
                    patch.object(checker, "ROOT", self.root),
                    patch.object(
                        checker,
                        "run_test",
                        side_effect=[completed(), completed(1, "FAIL: test")],
                    ) as run,
                ):
                    self.assertTrue(checker.run_case(name, case, suite))
                    self.assertEqual(
                        [call.args[1] for call in run.call_args_list],
                        [expected, expected],
                    )

    def test_installed_default_report_is_distinct_from_portable(self) -> None:
        for args, suite, evidence in (
            ([], "installed", "component_mutation_detection"),
            (
                ["--suite", "portable"],
                "portable",
                "portable_component_mutation_detection",
            ),
        ):
            output = io.StringIO()
            with (
                self.subTest(suite=suite),
                patch.object(checker, "run_case", return_value=True) as run,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(checker.main(args), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["evidence_class"], evidence)
            self.assertEqual(len(report["results"]), len(checker.CASES))
            self.assertTrue(all(call.args[2] == suite for call in run.call_args_list))
            if suite == "portable":
                self.assertEqual(report["native_acceptance"], "not_run")
            else:
                self.assertEqual(set(report), {"evidence_class", "results"})

    def test_undetected_mutation_keeps_suite_failed(self) -> None:
        for args in ([], ["--suite", "portable"]):
            with (
                patch.object(checker, "run_case", return_value=False),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(checker.main(args), 1)

    def test_child_preserves_optimization_mode(self) -> None:
        with patch.object(subprocess, "run", return_value=completed()) as run:
            checker.run_test(self.root, "synthetic_test")
        expected = [sys.executable]
        if sys.flags.optimize:
            expected.append("-" + "O" * sys.flags.optimize)
        self.assertEqual(
            run.call_args.args[0], [*expected, "-m", "unittest", "synthetic_test", "-q"]
        )


if __name__ == "__main__":
    unittest.main()
