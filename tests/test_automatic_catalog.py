"""Synthetic trusted catalog and real SQLite reservation boundaries."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.runner_fleet import automatic_catalog as c
from tools.runner_fleet.state import State


class CatalogTests(unittest.TestCase):
    def row(self) -> dict[str, Any]:
        return dict(
            repository="runspindle-core",
            suite="validation",
            source_sha="a" * 40,
            workflow_sha256="b" * 64,
            image_sha256="c" * 64,
            tool_closure_sha256="d" * 64,
            validation_manifest_sha256="e" * 64,
            image_path="/sealed/image.qcow2",
            manifest_path="/sealed/manifest.json",
            manifest_sha256="f" * 64,
            evidence_path="/sealed/evidence.json",
            evidence_sha256="0" * 64,
            required_checks=[
                "quality",
                "component",
                "rust",
                "mutation_1",
                "mutation_2",
                "mutation_3",
                "mutation_4",
                "aggregate",
            ],
        )

    def fixture(self) -> tuple[dict[str, Any], bytes, bytes]:
        row = self.row()
        manifest = json.dumps(
            dict(
                sha256=row["image_sha256"],
                candidate_path=row["image_path"],
                tool_closure_sha256=row["tool_closure_sha256"],
            )
        ).encode()
        evidence = {
            key: row[key]
            for key in [
                "repository",
                "suite",
                "source_sha",
                "workflow_sha256",
                "image_sha256",
                "tool_closure_sha256",
                "validation_manifest_sha256",
            ]
        }
        evidence.update(
            schema="qualified-workflow-v1",
            status="passed",
            isolation="passed",
            cleanup="verified",
            checks={name: "passed" for name in row["required_checks"]},
        )
        payload = json.dumps(evidence).encode()
        row["manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
        row["evidence_sha256"] = hashlib.sha256(payload).hexdigest()
        return row, manifest, payload

    def test_complete_exact_trusted_record(self) -> None:
        row, manifest, evidence = self.fixture()
        with patch.object(c, "sealed_bytes", side_effect=[manifest, evidence]):
            c.QualifiedRecord.parse(row).verify()

    def test_failed_missing_or_other_source_evidence_rejected(self) -> None:
        for change in [
            {"status": "failed"},
            {"checks": {"scaffold": "passed"}},
            {"source_sha": "9" * 40},
            {"isolation": "not_run"},
            {"cleanup": "uncertain"},
        ]:
            row, manifest, payload = self.fixture()
            evidence = json.loads(payload)
            evidence.update(change)
            payload = json.dumps(evidence).encode()
            row["evidence_sha256"] = hashlib.sha256(payload).hexdigest()
            with (
                patch.object(c, "sealed_bytes", side_effect=[manifest, payload]),
                self.assertRaises(ValueError),
            ):
                c.QualifiedRecord.parse(row).verify()

    def test_catalog_disabled_empty_duplicate_demo_fail_closed(self) -> None:
        row, _, _ = self.fixture()
        for value in [
            dict(
                schema="automatic-catalog-v1", activation_allowed=False, records=[row]
            ),
            dict(schema="automatic-catalog-v1", activation_allowed=True, records=[]),
            dict(
                schema="automatic-catalog-v1",
                activation_allowed=True,
                records=[row, row],
            ),
            dict(
                schema="automatic-catalog-v1",
                activation_allowed=True,
                records=[row | {"repository": "runspindle-demo-suite"}],
            ),
        ]:
            with (
                patch.object(
                    c, "sealed_bytes", return_value=json.dumps(value).encode()
                ),
                self.assertRaises(ValueError),
            ):
                c.load_catalog()

    def test_unsealed_file_and_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog"
            path.write_text("{}")
            with self.assertRaises(ValueError):
                c.sealed_bytes(path, 1024)
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                c.sealed_bytes(link, 1024)

    def test_atomic_single_strix_reservation_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            one = State(temporary)
            two = State(temporary)
            self.addCleanup(one.db.close)
            self.addCleanup(two.db.close)
            first = one.reserve(
                {"repository": "runspindle-core", "id": 1},
                single_strix=True,
                details={"automatic_fleet_v1": True},
            )
            self.assertEqual(first["slot"], 0)
            with self.assertRaises(RuntimeError):
                two.reserve(
                    {"repository": "runspindle-host", "id": 2}, single_strix=True
                )
            one.update(first, "uncertain")
            with self.assertRaises(RuntimeError):
                two.reserve(
                    {"repository": "runspindle-host", "id": 2}, single_strix=True
                )

    def test_single_strix_cannot_relax_mac_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = State(temporary)
            self.addCleanup(state.db.close)
            with self.assertRaises(ValueError):
                state.reserve(
                    {"repository": "runspindle-core", "id": 1},
                    capacity=1,
                    binding={"host": "macbook"},
                    single_strix=True,
                )
            self.assertEqual(state.active(), [])


if __name__ == "__main__":
    unittest.main()
