import copy
import unittest
from typing import Any

from tools.retention_dry_run import PROOFS, classify, unique


def sample() -> dict[str, Any]:
    identity = {
        "repository": "newdarwindev/runspindle-core",
        "head_sha": "a" * 40,
        "resource_id": "b" * 32,
        "run_id": 10,
        "run_attempt": 2,
        "job_id": 12,
    }
    return {
        "identity": identity,
        "overlay_allocated_bytes": 4_185_542_656,
        "compact_allocated_bytes": 4096,
        "proofs": {
            name: {"identity": copy.deepcopy(identity), **dict.fromkeys(fields, True)}
            for name, fields in PROOFS.items()
        },
    }


class RetentionDryRunTests(unittest.TestCase):
    def test_complete_synthetic_sample_estimates_but_does_not_reclaim(self) -> None:
        result = classify(sample())
        self.assertEqual(result["status"], "eligible_for_review")
        self.assertEqual(result["estimated_reclaimable_bytes"], 4_185_538_560)
        self.assertEqual(result["actual_freed_bytes"], 0)

    def test_github_success_is_not_teardown_or_readback(self) -> None:
        value = sample()
        value["proofs"] = {"github": value["proofs"]["github"]}
        value["compact_allocated_bytes"] = None
        result = classify(value)
        self.assertEqual(result["status"], "ineligible")
        self.assertEqual(result["estimated_reclaimable_bytes"], 0)

    def test_wrong_exact_identity_is_not_accepted(self) -> None:
        for field, wrong in (
            ("job_id", 40),
            ("run_attempt", 3),
            ("run_id", 8),
            ("head_sha", "c" * 40),
            ("resource_id", "d" * 32),
        ):
            with self.subTest(field=field):
                value = sample()
                value["proofs"]["teardown"]["identity"][field] = wrong
                self.assertEqual(classify(value)["reasons"], ["teardown_unverified"])

    def test_missing_absence_readback_hold_or_policy_stops_estimate(self) -> None:
        for proof, fields in PROOFS.items():
            for field in fields:
                with self.subTest(proof=proof, field=field):
                    value = sample()
                    value["proofs"][proof][field] = False
                    self.assertEqual(classify(value)["estimated_reclaimable_bytes"], 0)

    def test_malformed_sizes_and_duplicate_keys(self) -> None:
        value = sample()
        value["compact_allocated_bytes"] = True
        with self.assertRaises(ValueError):
            classify(value)
        with self.assertRaises(ValueError):
            unique([("identity", {}), ("identity", {})])


if __name__ == "__main__":
    unittest.main()
