"""Synthetic policy tests confer no runner or image qualification."""

import dataclasses
import unittest
from typing import Any

from tools.runner_fleet.automatic_policy import GIB, ImageRoute, capacity_one


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.route = ImageRoute(
            "runspindle-core", "a" * 64, "b" * 64, "c" * 64, "validation"
        )
        self.capacity = dict(
            occupied=0,
            available_memory=13 * GIB,
            filesystem_free=55 * GIB,
            pool_free=55 * GIB,
            pool_metadata_basis_points=7999,
            retention_allowance=25 * GIB,
        )

    def test_routes_exact_bindings_and_no_generic_labels(self) -> None:
        labels = self.route.labels()
        self.assertTrue(self.route.matches("runspindle-core", labels))
        for field, value in [
            ("repository", "runspindle-host"),
            ("image_sha256", "d" * 64),
            ("tool_closure_sha256", "d" * 64),
            ("validation_manifest_sha256", "d" * 64),
        ]:
            self.assertNotEqual(
                labels, dataclasses.replace(self.route, **{field: value}).labels()
            )
        for request in [
            [],
            ["self-hosted", "linux", "x64", "runspindle-isolated", "strix"],
            labels * 2,
            labels + ["self-hosted"],
            tuple(labels),
        ]:
            self.assertFalse(self.route.matches("runspindle-core", request))
        self.assertFalse(self.route.matches("runspindle-host", labels))

    def test_unqualified_fields_and_demo_rejected(self) -> None:
        for field, value in [
            ("repository", "runspindle-demo-suite"),
            ("image_sha256", "latest"),
            ("tool_closure_sha256", "A" * 64),
            ("suite", "scaffold"),
            ("suite", "browser-account-e2e"),
        ]:
            with self.assertRaises(ValueError):
                dataclasses.replace(self.route, **{field: value}).labels()

    def test_capacity_is_at_most_one(self) -> None:
        self.assertEqual(capacity_one(**self.capacity), 1)
        for occupied in [1, 2, 100]:
            self.assertEqual(
                capacity_one(**(self.capacity | {"occupied": occupied})), 0
            )

    def test_each_independent_budget_blocks(self) -> None:
        for key in [
            "available_memory",
            "filesystem_free",
            "pool_free",
            "retention_allowance",
        ]:
            self.assertEqual(
                capacity_one(**(self.capacity | {key: self.capacity[key] - 1})), 0
            )
        self.assertEqual(
            capacity_one(**(self.capacity | {"pool_metadata_basis_points": 8000})), 0
        )

    def test_invalid_observation_never_admitted(self) -> None:
        for value in [-1, True, 1.0, None]:
            with self.assertRaises(ValueError):
                invalid: dict[str, Any] = self.capacity | {"pool_free": value}
                capacity_one(**invalid)


if __name__ == "__main__":
    unittest.main()
