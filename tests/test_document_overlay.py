"""Mocked offline recipe and sealing paths; never execute guest/host commands."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet import document_candidate as dc
from test_document_candidate import Fixture


class PlanEntryTests(Fixture):
    def test_wal_snapshot_uses_committed_wal_without_original_writes(self) -> None:
        state = self.root / "wal-state"
        state.mkdir()
        database = state / "fleet.sqlite"
        db = sqlite3.connect(database)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.executescript(
                "CREATE TABLE control(key TEXT,value TEXT); CREATE TABLE resources(phase TEXT);"
            )
            db.execute("INSERT INTO control VALUES('drained','false')")
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("UPDATE control SET value='true' WHERE key='drained'")
            db.commit()  # Only the WAL has the accepted fresh value.
            before = {path.name: path.read_bytes() for path in state.iterdir()}
            dc.read_idle_state(state)
            self.assertEqual(
                {path.name: path.read_bytes() for path in state.iterdir()}, before
            )
            db.execute("UPDATE control SET value='false' WHERE key='drained'")
            db.commit()
            with self.assertRaisesRegex(RuntimeError, "drained"):
                dc.read_idle_state(state)
        finally:
            db.close()
        before = {path.name: path.read_bytes() for path in state.iterdir()}
        with self.assertRaisesRegex(RuntimeError, "drained"):
            dc.read_idle_state(state)
        self.assertEqual(
            {path.name: path.read_bytes() for path in state.iterdir()}, before
        )
        self.assertEqual(set(before), {"fleet.sqlite"})

    def test_real_entry_point_has_no_import_or_state_writes(self) -> None:
        source = Path(__file__).resolve().parents[1]
        copied = self.root / "source"
        for name in (
            "tools/document_candidate.py",
            *(
                "tools/runner_fleet/" + part
                for part in (
                    "__init__.py",
                    "document_candidate.py",
                    "supervision.py",
                    "backend.py",
                    "state.py",
                    "identity.py",
                    "documents.py",
                    "quality.py",
                    "model.py",
                )
            ),
        ):
            target = copied / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        p = self.p | {
            "base_image": "/var/lib/runspindle-fleet/images/qualified.qcow2",
            "base_manifest": "/var/lib/runspindle-fleet/images/qualified.json",
            "protected": {"/var/lib/runspindle-fleet/images/qualified.qcow2": "2" * 64},
        }
        plan_path = self.root / "plan.json"
        plan_path.write_text(json.dumps(p))
        before = {
            str(path): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        result = subprocess.run(
            [
                sys.executable,
                str(copied / "tools/document_candidate.py"),
                "--plan",
                str(plan_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "plan_only")
        self.assertEqual(
            {
                str(path): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
            before,
        )
        self.assertFalse(any(self.root.rglob("__pycache__")))


class OverlayTests(Fixture):
    def setUp(self) -> None:
        super().setUp()
        self.operation, self.image, self.manifest = dc.paths(self.p)
        self.operation.mkdir(parents=True)
        self.resource = {"id": "a" * 32, "slot": 0}
        self.backend: Any = Mock()
        self.backend.ready.return_value = True
        self.backend.slot.return_value = self.root / "slots/0"
        self.guest = {
            "manifest_kind": "document-overlay-v1",
            "base_guest": {"identity": "synthetic base"},
            "operation_id": self.p["operation_id"],
            "base_image_sha256": self.p["base_sha256"],
            "base_manifest_sha256": self.p["base_manifest_sha256"],
            "document_closure_sha256": self.p["document_closure_sha256"],
            "execution_source_sha": self.p["execution_source_sha"],
            "documents": {"closure_sha256": self.p["document_closure_sha256"]},
            "preparation_source_sha256": {
                name: dc.digest(Path(__file__).resolve().parents[1] / name)
                for name in dc.DOCUMENT_SOURCES
            },
        }
        Path(self.p["base_manifest"]).write_text(
            json.dumps({"guest": self.guest["base_guest"]})
        )
        self.enterContext(patch.object(dc, "verify_source"))
        self.enterContext(patch.object(dc, "regular"))
        self.enterContext(patch.object(dc, "verify_documents"))
        self.enterContext(
            patch.object(
                dc,
                "load_documents",
                return_value={
                    "wheels": [{"filename": f"synthetic{i}.whl"} for i in range(3)]
                },
            )
        )

    def test_preparation_transfers_only_closed_document_inputs(self) -> None:
        self.backend.ssh.return_value = Mock(
            returncode=0,
            stdout="RUNSPINDLE_DOCUMENT_OVERLAY=" + json.dumps(self.guest) + "\n",
            stderr="",
        )
        dc.prepare_overlay(self.p, self.backend, self.resource)
        transfers = [c.args[2] for c in self.backend.copy_prepared.call_args_list]
        self.assertEqual(
            transfers,
            [
                "base-guest.json",
                "overlay-context.json",
                "requirements-documents.txt",
                *(f"document-wheels/synthetic{i}.whl" for i in range(3)),
            ],
        )
        self.assertEqual(
            json.loads((self.operation / "guest.json").read_text()), self.guest
        )
        script = self.backend.ssh.call_args.args[1]
        self.assertIn(
            "--no-index --no-deps --only-binary=:all: --require-hashes", script
        )
        self.assertNotIn("apt-get", script)
        self.assertNotIn("curl ", script)
        self.assertNotIn("docker ", script)
        self.assertLess(
            script.index("python3 -m runner_fleet.documents"),
            script.index("sudo python3 -m venv"),
        )

    def test_lost_ssh_result_cannot_acknowledge_preparation(self) -> None:
        self.backend.ssh.return_value = Mock(
            returncode=255,
            stdout="RUNSPINDLE_DOCUMENT_OVERLAY=" + json.dumps(self.guest),
            stderr="connection lost",
        )
        with self.assertRaisesRegex(RuntimeError, "acknowledge"):
            dc.prepare_overlay(self.p, self.backend, self.resource)
        self.assertFalse((self.operation / "guest.json").exists())

    def test_mismatched_guest_base_manifest_is_refused(self) -> None:
        wrong = self.guest | {"base_guest": {"identity": "other base"}}
        self.backend.ssh.return_value = Mock(
            returncode=0,
            stdout="RUNSPINDLE_DOCUMENT_OVERLAY=" + json.dumps(wrong),
            stderr="",
        )
        with self.assertRaisesRegex(RuntimeError, "differs"):
            dc.prepare_overlay(self.p, self.backend, self.resource)

    def observed(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource["id"],
            "ActiveState": "inactive",
            "MainPID": "0",
        }

    def test_unacknowledged_hygiene_cannot_seal(self) -> None:
        self.backend.ssh.return_value = Mock(returncode=255, stdout="", stderr="lost")
        with (
            self.assertRaisesRegex(RuntimeError, "hygiene"),
            patch.object(dc, "execute") as execute,
        ):
            dc.seal_overlay(self.p, self.backend, self.resource)
        execute.assert_not_called()

    def test_foreign_ownership_cannot_seal(self) -> None:
        self.backend.ssh.return_value = Mock(
            returncode=0, stdout="RUNSPINDLE_OVERLAY_HYGIENE=complete\n", stderr=""
        )
        with patch.object(
            dc, "helper", return_value=self.observed() | {"resource_id": "foreign"}
        ):
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                dc.seal_overlay(self.p, self.backend, self.resource)

    def test_seal_rejects_symlink_before_host_helper_can_remove_it(self) -> None:
        self.backend.ssh.return_value = Mock(
            returncode=0, stdout="RUNSPINDLE_OVERLAY_HYGIENE=complete\n", stderr=""
        )
        target = self.root / "preserved"
        target.write_text("keep")
        linked = self.root / "slots/0/disk.qcow2"
        linked.symlink_to(target)
        with patch.object(dc, "helper", return_value=self.observed()) as helper:
            with self.assertRaisesRegex(RuntimeError, "Unexpected slot"):
                dc.seal_overlay(self.p, self.backend, self.resource)
        self.assertEqual([c.args[0] for c in helper.call_args_list], ["observe"])
        self.assertTrue(linked.is_symlink())
        self.assertEqual(target.read_text(), "keep")

    def test_seal_is_exclusive_typed_and_preserves_base(self) -> None:
        self.backend.ssh.return_value = Mock(
            returncode=0, stdout="RUNSPINDLE_OVERLAY_HYGIENE=complete\n", stderr=""
        )
        (self.operation / "guest.json").write_text(json.dumps(self.guest))
        base_before = Path(self.p["base_manifest"]).read_bytes()
        original_stat = Path.stat

        def root_image_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
            result = original_stat(path, *args, **kwargs)
            if path == self.image:
                fields = list(result)
                fields[4] = 0
                return os.stat_result(fields)
            return result

        def command(argv: list[str], **kwargs: Any) -> Mock:
            if argv[0] == "pgrep":
                return Mock(returncode=1)
            if argv[:3] == ["sudo", "-n", "mkdir"]:
                self.image.parent.mkdir()
            elif argv[:4] == ["sudo", "-n", "qemu-img", "convert"]:
                self.assertEqual(argv[-1], str(self.image))
                self.image.write_bytes(b"synthetic flattened overlay")
            elif argv[:3] == ["sudo", "-n", "chmod"]:
                self.image.chmod(0o444)
            else:
                raise AssertionError("unexpected command")
            return Mock(returncode=0)

        with (
            patch.object(dc, "helper", return_value=self.observed()),
            patch.object(dc, "execute", side_effect=command),
            patch.object(Path, "stat", root_image_stat),
        ):
            dc.seal_overlay(self.p, self.backend, self.resource)
            # Reusing an operation fails before qemu-img can overwrite its output.
            with self.assertRaises(FileExistsError):
                dc.seal_overlay(self.p, self.backend, self.resource)
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual(manifest["manifest_kind"], "document-overlay-v1")
        self.assertFalse(manifest["ordinary_fleet_eligible"])
        self.assertFalse(manifest["full_recipe_rebuild"])
        self.assertEqual(
            manifest["base_manifest_sha256"], self.p["base_manifest_sha256"]
        )
        self.assertEqual(manifest["validation_sources"], self.p["sources"])
        self.assertEqual(Path(self.p["base_manifest"]).read_bytes(), base_before)
        self.assertFalse((self.root / "state/candidate.json").exists())


if __name__ == "__main__":
    unittest.main()
