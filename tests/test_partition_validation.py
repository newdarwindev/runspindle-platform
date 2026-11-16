"""Negative tests for exact-source aggregate CI receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import hygiene  # noqa: E402
from tools import partition_validation as gate  # noqa: E402


class AggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest, self.inventory, self.manifest_sha, self.hygiene_sha = (
            gate.manifests()
        )
        # Synthetic qualification only for aggregate contract tests. The real
        # manifest remains explicitly unqualified for the installed ARM64 gate.
        arm_image = copy.deepcopy(self.manifest["images"]["linux-x64"])
        arm_image["platform"] = "linux/arm64"
        arm_image["pins"] = {
            key: ("f" * 40 if key == "source_sha" else "f" * 64)
            for key in arm_image["pins"]
        }
        self.manifest["images"]["synthetic-arm64"] = arm_image
        self.manifest["contracts"]["mutation_2"].update(
            qualification="pinned", image="synthetic-arm64"
        )
        self.manifest_sha = hashlib.sha256(
            json.dumps(self.manifest).encode()
        ).hexdigest()
        self.image_sha = self.manifest["images"]["linux-x64"]["pins"]["sha256"]
        self.head = "a" * 40
        self.run_id = "12345"
        by_id = {x["id"]: x for x in self.inventory["checks"]}
        self.receipts = {}
        for partition in gate.PARTITIONS:
            checks = []
            for identity in self.manifest["partitions"][partition]:
                entry = by_id[identity]
                if "not_applicable" in entry:
                    checks.append(
                        {
                            "id": identity,
                            "status": "not_applicable",
                            "reason": entry["not_applicable"],
                        }
                    )
                else:
                    checks.append(
                        {
                            "id": identity,
                            "status": "passed",
                            "exit_code": 0,
                            "command_sha256": hashlib.sha256(
                                json.dumps(entry["command"]).encode()
                            ).hexdigest(),
                        }
                    )
            contract, image = gate.contract_image(self.manifest, partition)
            self.receipts[partition] = {
                "schema_version": 2,
                "platform": contract["platform"],
                "contract_sha256": gate.contract_digest(contract, image),
                "partition": partition,
                "run_id": self.run_id,
                "source_sha": self.head,
                "manifest_sha256": self.manifest_sha,
                "hygiene_sha256": self.hygiene_sha,
                "image_sha256": image["sha256"],
                "binding": {
                    "image_source_sha": image["source_sha"],
                    **{
                        k: image[k]
                        for k in (
                            "controller_sha256",
                            "recipe_sha256",
                            "tool_closure_sha256",
                        )
                    },
                },
                "source_fingerprint": "b" * 64,
                "preflight": {
                    "id": "reproducible-inputs",
                    "status": "passed",
                    "problems": [],
                }
                if partition == "quality"
                else None,
                "checks": checks,
                "status": "passed",
            }
        self.results = {x: "success" for x in gate.PARTITIONS}

    def check(
        self,
        receipts: dict[str, dict[str, Any]] | None = None,
        results: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return gate.aggregate(
            receipts if receipts is not None else self.receipts,
            results if results is not None else self.results,
            self.manifest,
            self.inventory,
            self.manifest_sha,
            self.hygiene_sha,
            self.image_sha,
            self.head,
            self.run_id,
        )

    def test_complete_receipts(self) -> None:
        self.assertEqual(self.check()["status"], "passed")

    def test_synthetic_arm64_producer_round_trips_own_contract(self) -> None:
        expected = self.receipts["mutation_2"]
        instance = {"image_sha256": expected["image_sha256"], **expected["binding"]}
        with (
            patch.object(gate, "observed_platform", return_value="linux/arm64"),
            patch.object(hygiene, "source_fingerprint", return_value="b" * 64),
            patch.object(hygiene, "run_entry", return_value=expected["checks"][0]),
        ):
            produced = gate.receipt(
                "mutation_2",
                self.manifest,
                self.inventory,
                self.manifest_sha,
                self.hygiene_sha,
                instance,
                self.head,
                self.run_id,
            )
        self.assertEqual(produced, expected)
        self.assertEqual(
            self.check(self.receipts | {"mutation_2": produced})["status"], "passed"
        )
        with (
            patch.object(gate, "observed_platform", return_value="linux/arm64"),
            patch.object(hygiene, "run_entry") as child,
        ):
            with self.assertRaisesRegex(ValueError, "binding"):
                gate.receipt(
                    "mutation_2",
                    self.manifest,
                    self.inventory,
                    self.manifest_sha,
                    self.hygiene_sha,
                    instance | {"image_sha256": self.image_sha},
                    self.head,
                    self.run_id,
                )
            child.assert_not_called()

    def test_unqualified_contract_cannot_be_forged_by_success_receipt(self) -> None:
        self.manifest["contracts"]["mutation_2"].update(
            qualification="unqualified", image=None
        )
        with self.assertRaisesRegex(ValueError, "unqualified"):
            self.check()

    def test_partition_receipt_binds_its_own_image_and_platform(self) -> None:
        for field, value in (
            ("schema_version", 1),
            ("platform", "linux/amd64"),
            ("contract_sha256", "0" * 64),
            ("image_sha256", self.image_sha),
            ("binding", self.receipts["quality"]["binding"]),
        ):
            receipts = copy.deepcopy(self.receipts)
            receipts["mutation_2"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(receipts)
        for field in self.receipts["mutation_2"]["binding"]:
            receipts = copy.deepcopy(self.receipts)
            receipts["mutation_2"]["binding"][field] = "0" * 64
            with self.subTest(binding=field), self.assertRaises(ValueError):
                self.check(receipts)

    def test_skipped_installed_check_or_job_is_not_success(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        receipts["mutation_2"]["checks"][0]["status"] = "skipped"
        with self.assertRaises(ValueError):
            self.check(receipts)
        with self.assertRaises(ValueError):
            self.check(results=self.results | {"mutation_2": "skipped"})

    def test_missing_cancelled_and_stale(self) -> None:
        for field, value in [
            ("source_sha", "c" * 40),
            ("image_sha256", "d" * 64),
            ("manifest_sha256", "e" * 64),
            ("run_id", "999"),
        ]:
            receipts = copy.deepcopy(self.receipts)
            receipts["quality"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(receipts)
        results = self.results | {"rust": "cancelled"}
        with self.assertRaises(ValueError):
            self.check(results=results)
        receipts = copy.deepcopy(self.receipts)
        del receipts["mutation_4"]
        with self.assertRaises(ValueError):
            self.check(receipts)

    def test_duplicate_missing_and_forged_checks(self) -> None:
        name = next(x for x in gate.PARTITIONS if self.receipts[x]["checks"])
        for change in ("missing", "duplicate", "forged"):
            receipts = copy.deepcopy(self.receipts)
            if change == "missing":
                receipts[name]["checks"].pop()
            elif change == "duplicate":
                receipts[name]["checks"].append(receipts[name]["checks"][0])
            else:
                check = next(
                    (x for x in receipts[name]["checks"] if x["status"] == "passed"),
                    None,
                )
                if check is None:
                    continue
                check["command_sha256"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.check(receipts)

    def test_preflight_required(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        receipts["quality"]["preflight"]["status"] = "failed"
        with self.assertRaises(ValueError):
            self.check(receipts)

    def test_partition_source_fingerprints_must_match(self) -> None:
        receipts = copy.deepcopy(self.receipts)
        receipts["rust"]["source_fingerprint"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "source bytes differ"):
            self.check(receipts)

    def test_guest_binding_requires_observed_recipe_and_controller(self) -> None:
        image = self.manifest["images"]["linux-x64"]["pins"]
        observed = {
            "image_sha256": image["sha256"],
            "image_source_sha": image["source_sha"],
            "controller_sha256": image["controller_sha256"],
            "recipe_sha256": image["recipe_sha256"],
            "tool_closure_sha256": image["tool_closure_sha256"],
        }
        gate.validate_instance_binding(observed, image)
        observed["recipe_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "binding"):
            gate.validate_instance_binding(observed, image)
        observed["recipe_sha256"] = image["recipe_sha256"]
        observed["image_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "binding"):
            gate.validate_instance_binding(observed, image)

    def test_failure_summary_is_bounded_and_sanitized(self) -> None:
        lines = hygiene.failure_summary(
            {"checks": [{"id": "rust-test", "status": "failed", "exit_code": 2}]}
        )
        self.assertEqual(
            lines,
            [
                "hygiene failure: check=rust-test exit=2 reported=unclassified (unverified)"
            ],
        )

    def test_manifest_missing_partition_and_duplicate_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hygiene.json").write_bytes(
                (gate.ROOT / "hygiene.json").read_bytes()
            )
            manifest = copy.deepcopy(self.manifest)
            del manifest["partitions"]["mutation_4"]
            (root / "validation-partitions.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "partition manifest"):
                gate.manifests(root)
            manifest = copy.deepcopy(self.manifest)
            identity = next(x for x in gate.PARTITIONS if manifest["partitions"][x])
            manifest["partitions"][identity].append(manifest["partitions"][identity][0])
            (root / "validation-partitions.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "duplicate mandatory"):
                gate.manifests(root)


class PlatformContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest, self.inventory, self.manifest_sha, self.hygiene_sha = (
            gate.manifests()
        )

    def test_real_arm64_contract_is_valid_but_unqualified(self) -> None:
        gate.validate_contracts(self.manifest)
        self.assertEqual(
            self.manifest["contracts"]["mutation_2"],
            {
                "platform": "linux/arm64",
                "qualification": "unqualified",
                "image": None,
            },
        )
        for name in gate.PARTITIONS:
            if name == "mutation_2":
                with self.assertRaisesRegex(ValueError, "unqualified"):
                    gate.contract_image(self.manifest, name)
            else:
                contract, image = gate.contract_image(self.manifest, name)
                self.assertEqual(contract["platform"], "linux/amd64")
                self.assertEqual(image, self.manifest["images"]["linux-x64"]["pins"])

    def test_missing_platform_qualification_or_image_never_defaults(self) -> None:
        for field in ("platform", "qualification", "image"):
            manifest = copy.deepcopy(self.manifest)
            del manifest["contracts"]["quality"][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.validate_contracts(manifest)
        for field, value in (
            ("image", "unknown"),
            ("qualification", "qualified"),
            ("platform", "linux/arm64"),
        ):
            manifest = copy.deepcopy(self.manifest)
            manifest["contracts"]["quality"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.validate_contracts(manifest)

    def test_missing_partition_and_legacy_schema_rejected(self) -> None:
        for change in ("legacy", "missing", "aggregate", "installed_platform"):
            manifest = copy.deepcopy(self.manifest)
            if change == "legacy":
                manifest["schema_version"] = 1
            elif change == "missing":
                del manifest["contracts"]["mutation_2"]
            elif change == "aggregate":
                del manifest["aggregate_contract"]
            else:
                manifest["contracts"]["mutation_2"]["platform"] = "linux/amd64"
            with self.subTest(change=change), self.assertRaises(ValueError):
                gate.validate_contracts(manifest)

    def test_image_pins_and_platform_are_explicit(self) -> None:
        for field in self.manifest["images"]["linux-x64"]["pins"]:
            manifest = copy.deepcopy(self.manifest)
            del manifest["images"]["linux-x64"]["pins"][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.validate_contracts(manifest)
        manifest = copy.deepcopy(self.manifest)
        del manifest["images"]["linux-x64"]["platform"]
        with self.assertRaises(ValueError):
            gate.validate_contracts(manifest)

    def test_execution_rejected_before_any_checks_or_metadata(self) -> None:
        for name, observed in (
            ("mutation_2", "linux/arm64"),
            ("mutation_2", "linux/amd64"),
            ("quality", "linux/arm64"),
            ("quality", "unsupported"),
        ):
            with (
                self.subTest(name=name, observed=observed),
                patch.object(gate, "observed_platform", return_value=observed),
                patch.object(hygiene, "source_fingerprint") as fingerprint,
                patch.object(hygiene, "preflight") as preflight,
                patch.object(hygiene, "run_entry") as child,
            ):
                with self.assertRaises(ValueError):
                    gate.receipt(
                        name,
                        self.manifest,
                        self.inventory,
                        self.manifest_sha,
                        self.hygiene_sha,
                        {},
                        "a" * 40,
                        "1",
                    )
                fingerprint.assert_not_called()
                preflight.assert_not_called()
                child.assert_not_called()

    def test_observation_does_not_qualify_unknown_or_nonlinux_architecture(
        self,
    ) -> None:
        for system, machine, expected in (
            ("Linux", "x86_64", "linux/amd64"),
            ("Linux", "aarch64", "linux/arm64"),
            ("Darwin", "arm64", "unsupported"),
            ("Linux", "unknown", "unsupported"),
        ):
            with (
                patch.object(platform, "system", return_value=system),
                patch.object(platform, "machine", return_value=machine),
            ):
                self.assertEqual(gate.observed_platform(), expected)


if __name__ == "__main__":
    unittest.main()
