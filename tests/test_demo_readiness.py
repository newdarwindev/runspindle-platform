"""Source-only proposal tests; no deployment or runner qualification evidence."""

import copy
import unittest

from tools.runner_fleet import model
from tools.runner_fleet.demo_readiness import (
    REQUIRED_TOOLS,
    prepare,
    validate_candidate,
)


class DemoReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = model.config()

    def test_paired_candidate_does_not_modify_active_config_or_policy(self) -> None:
        before = copy.deepcopy(self.config)
        plan = prepare(self.config, {}, None, None)
        self.assertEqual(self.config, before)
        self.assertEqual(
            plan["candidate_config"]["repositories"],
            before["repositories"] + ["runspindle-demo-suite"],
        )
        self.assertEqual(plan["candidate_config"]["lanes"], before["lanes"])
        self.assertFalse(plan["activation_allowed"])
        self.assertEqual(model.config(), before)
        with self.assertRaises(ValueError):
            model.validate_config(plan["candidate_config"])

    def test_mismatched_policy_config_and_extra_repositories_fail_closed(self) -> None:
        plan = prepare(self.config, {}, None, None)
        cfg, policy = plan["candidate_config"], plan["candidate_policy"]
        self.assertEqual(validate_candidate(cfg, policy), cfg)
        with self.assertRaises(ValueError):
            validate_candidate(self.config, policy)
        for target in ["candidate_config", "candidate_policy"]:
            changed = copy.deepcopy(plan)
            changed[target]["repositories"].append("unexpected-repository")
            with self.assertRaises(ValueError):
                validate_candidate(
                    changed["candidate_config"], changed["candidate_policy"]
                )
        for activation in [True, 0, None]:
            policy["activation_allowed"] = activation
            with self.assertRaises(ValueError):
                validate_candidate(cfg, policy)
        plan = prepare(self.config, {}, None, None)
        plan["candidate_config"]["lanes"]["strix-linux-x64"]["labels"].remove(
            "runspindle-isolated"
        )
        with self.assertRaises(ValueError):
            validate_candidate(plan["candidate_config"], plan["candidate_policy"])

    def test_missing_tools_and_wrong_browser_report_blockers(self) -> None:
        plan = prepare(self.config, {}, "1248", "1.56.1")
        self.assertEqual(len(plan["prerequisite_blockers"]), len(REQUIRED_TOOLS) + 1)

    def test_ready_tools_never_authorize_activation(self) -> None:
        plan = prepare(
            self.config, dict.fromkeys(REQUIRED_TOOLS, True), "1194", "1.56.1"
        )
        self.assertEqual(plan["prerequisite_blockers"], [])
        self.assertFalse(plan["activation_allowed"])

    def test_wrong_owner_and_lane_rejected(self) -> None:
        self.config["owner"] = "other"
        with self.assertRaises(ValueError):
            prepare(self.config, {}, None, None)
        self.config["owner"] = "newdarwindev"
        self.config["lanes"]["strix-linux-x64"]["architecture"] = "ARM64"
        with self.assertRaises(ValueError):
            prepare(self.config, {}, None, None)
