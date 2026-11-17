"""Synthetic observation tests: no GitHub changes or host network operations."""

import base64
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.runner_fleet import bounded_selection as selection

HEAD = "1" * 40
WORKFLOW = ".github/workflows/example.yml"
FULL = "newdarwindev/runspindle-platform"


class Fixture:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.run = {
            "id": 10,
            "run_attempt": 1,
            "head_sha": HEAD,
            "path": WORKFLOW,
            "status": "queued",
            "event": "push",
            "head_branch": "main",
            "head_repository": {"full_name": FULL},
        }
        self.job = {
            "id": 20,
            "run_id": 10,
            "head_sha": HEAD,
            "status": "queued",
            "name": "example",
            "labels": selection.LABELS.copy(),
            "runner_id": 0,
            "runner_name": "",
        }
        raw = b"name: example\n"
        self.content = {
            "path": WORKFLOW,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
            "sha": hashlib.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw
            ).hexdigest(),
        }
        self.runs: dict[str, Any] = {"total_count": 1, "workflow_runs": [self.run]}
        self.jobs: dict[str, Any] = {"total_count": 1, "jobs": [self.job]}
        self.current_run = self.run
        self.current_job = self.job
        self.head = HEAD
        self.nft: dict[str, Any] = {
            "nftables": [{"metainfo": {"json_schema_version": 1}}]
            + selection.expected_nft()
        }
        for row in self.nft["nftables"][1:]:
            body = next(iter(row.values()))
            body["handle"] = 42
            for expr in body.get("expr", []):
                if "counter" in expr:
                    expr["counter"] = {"packets": 1, "bytes": 60}
        self.links: list[list[dict[str, Any]]] = []
        for slot in range(2):
            self.links.append(
                [
                    {
                        "ifname": f"rsvm{slot}",
                        "flags": ["UP"],
                        "linkinfo": {
                            "info_kind": "tun",
                            "info_data": {
                                "type": "tap",
                                "user": f"runspindle-vm{slot}",
                                "persist": True,
                            },
                        },
                        "addr_info": [
                            {
                                "family": "inet",
                                "local": f"10.231.{slot}.1",
                                "prefixlen": 30,
                            }
                        ],
                    }
                ]
            )
        self.service = "ActiveState=active\nSubState=running\n"

    def read(self, argv: list[str]) -> str:
        self.commands.append(argv)
        if argv[0] == "/usr/bin/gh":
            if argv[1:6] != ["api", "--hostname", "github.com", "--method", "GET"]:
                raise AssertionError("Not read-only")
            path = argv[6].removeprefix("repos/" + FULL + "/")
            value = {
                "git/ref/heads/main": {"object": {"sha": self.head}},
                "actions/runs?status=queued&per_page=100": self.runs,
                "actions/runs/10/attempts/1/jobs?per_page=100": self.jobs,
                "contents/" + WORKFLOW + "?ref=" + HEAD: self.content,
                "actions/runs/10": self.current_run,
                "actions/jobs/20": self.current_job,
            }[path]
            return json.dumps(value)
        if argv[:5] == ["/usr/bin/sudo", "-n", "/usr/sbin/nft", "-j", "list"]:
            return json.dumps(self.nft)
        if argv[:6] == ["/usr/sbin/ip", "-j", "-d", "address", "show", "dev"]:
            return json.dumps(self.links[int(argv[-1][-1])])
        if argv[:2] == ["/usr/bin/systemctl", "show"]:
            return self.service
        raise AssertionError("Unexpected or mutating command")

    def inspect(self) -> dict[str, Any]:
        return selection.inspect(
            "runspindle-platform", HEAD, WORKFLOW, "example", self.read
        )


class BoundedSelectionTests(unittest.TestCase):
    def test_exact_selection_is_observation_never_authority(self) -> None:
        fixture = Fixture()
        result = fixture.inspect()
        self.assertEqual(result["selected"]["job_id"], 20)
        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["network"]["traffic_tested"])
        self.assertFalse(result["network"]["service_bytes_attested"])
        self.assertEqual(result["dispatches"], 0)
        self.assertEqual(result["registrations"], 0)
        self.assertLessEqual(len(fixture.commands), 18)

    def test_unknown_repository_and_noncurrent_source_fail_before_network(self) -> None:
        fixture = Fixture()
        with self.assertRaises(ValueError):
            selection.inspect("unrelated", HEAD, WORKFLOW, "example", fixture.read)
        self.assertFalse(fixture.commands)
        fixture.head = "2" * 40
        with self.assertRaises(ValueError):
            fixture.inspect()
        self.assertEqual(len(fixture.commands), 1)

    def test_ambiguous_run_is_not_guessed(self) -> None:
        fixture = Fixture()
        fixture.runs["workflow_runs"].append(copy.deepcopy(fixture.run))
        fixture.runs["total_count"] = 2
        with self.assertRaisesRegex(ValueError, "exactly one"):
            fixture.inspect()
        self.assertEqual(len(fixture.commands), 2)

    def test_queue_and_job_truncation_fail(self) -> None:
        for name in ("runs", "jobs"):
            fixture = Fixture()
            getattr(fixture, name)["total_count"] = 101
            with self.assertRaisesRegex(ValueError, "incomplete"):
                fixture.inspect()

    def test_untrusted_or_changed_runs_fail(self) -> None:
        for field, value in [
            ("event", "pull_request"),
            ("head_branch", "foreign"),
            ("head_repository", {"full_name": "other/repository"}),
            ("run_attempt", True),
        ]:
            fixture = Fixture()
            fixture.run[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                fixture.inspect()

    def test_job_identity_labels_assignment_and_duplicates_fail(self) -> None:
        cases: list[tuple[str, Any]] = [
            ("id", True),
            ("head_sha", "2" * 40),
            ("run_id", 30),
            ("labels", ["self-hosted"]),
            ("runner_id", 1),
            ("runner_id", False),
            ("runner_name", "already-running"),
        ]
        for field, value in cases:
            fixture = Fixture()
            fixture.job[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                fixture.inspect()
        fixture = Fixture()
        fixture.jobs["jobs"].append(copy.deepcopy(fixture.job))
        fixture.jobs["total_count"] = 2
        with self.assertRaises(ValueError):
            fixture.inspect()

    def test_recheck_rejects_attempt_assignment_and_source_drift(self) -> None:
        for name, field, value in [
            ("current_run", "run_attempt", 2),
            ("current_run", "path", ".github/workflows/other.yml"),
            ("current_job", "runner_id", 123),
            ("current_job", "labels", ["other"]),
            ("current_job", "id", 99),
        ]:
            fixture = Fixture()
            setattr(fixture, name, copy.deepcopy(getattr(fixture, name)))
            getattr(fixture, name)[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "stale"):
                fixture.inspect()

    def test_workflow_blob_bytes_are_checked(self) -> None:
        fixture = Fixture()
        fixture.content["content"] = base64.b64encode(b"other bytes").decode()
        with self.assertRaisesRegex(ValueError, "blob mismatch"):
            fixture.inspect()

    def test_firewall_missing_extra_and_permissive_rule_fail(self) -> None:
        fixture = Fixture()
        fixture.nft["nftables"][-1]["rule"]["expr"][-1] = {"accept": None}
        with self.assertRaisesRegex(ValueError, "firewall differs"):
            fixture.inspect()
        for delta in (-1, 1):
            fixture = Fixture()
            if delta == -1:
                fixture.nft["nftables"].pop()
            else:
                fixture.nft["nftables"].append(
                    copy.deepcopy(fixture.nft["nftables"][-1])
                )
            with self.assertRaises(ValueError):
                fixture.inspect()

    def test_port_change_and_counter_schema_fail(self) -> None:
        fixture = Fixture()
        fixture.nft["nftables"][7]["rule"]["expr"][2]["match"]["right"] = 22
        with self.assertRaises(ValueError):
            fixture.inspect()
        fixture = Fixture()
        fixture.nft["nftables"][8]["rule"]["expr"][1]["counter"]["bytes"] = True
        with self.assertRaises(ValueError):
            fixture.inspect()

    def test_down_bridged_wrong_owner_and_extra_address_fail(self) -> None:
        fixture = Fixture()
        for field, value in [("flags", []), ("master", "foreign")]:
            fixture = Fixture()
            fixture.links[0][0][field] = value
            with self.assertRaises(ValueError):
                fixture.inspect()
        fixture = Fixture()
        fixture.links[0][0]["linkinfo"]["info_data"]["user"] = "other"
        with self.assertRaises(ValueError):
            fixture.inspect()
        fixture = Fixture()
        fixture.links[0][0]["addr_info"].append(
            {"family": "inet", "local": "192.0.2.1", "prefixlen": 24}
        )
        with self.assertRaises(ValueError):
            fixture.inspect()

    def test_inactive_service_is_not_started(self) -> None:
        fixture = Fixture()
        fixture.service = "ActiveState=inactive\nSubState=dead\n"
        with self.assertRaises(ValueError):
            fixture.inspect()
        self.assertTrue(all("start" not in argv for argv in fixture.commands))

    def test_execute_refuses_before_observation(self) -> None:
        argv = [
            "probe",
            "--repository",
            "runspindle-platform",
            "--source-sha",
            HEAD,
            "--workflow",
            WORKFLOW,
            "--job-name",
            "example",
            "--execute",
        ]
        with (
            patch.object(sys, "argv", argv),
            patch.object(selection, "inspect") as inspect,
        ):
            with self.assertRaises(SystemExit):
                selection.main()
            inspect.assert_not_called()

    def test_output_bound_and_nonzero_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "capture bound"):
            selection.read_command(
                [sys.executable, "-c", 'import sys; sys.stdout.write("x" * 3000000)']
            )
        with self.assertRaises(RuntimeError):
            selection.read_command([sys.executable, "-c", "raise SystemExit(2)"])

    def test_network_observation_cannot_hide_job_drift(self) -> None:
        fixture = Fixture()

        def changing(argv: list[str]) -> str:
            value = fixture.read(argv)
            if argv[0] == "/usr/bin/systemctl":
                fixture.job["runner_id"] = 900
            return value

        with self.assertRaisesRegex(ValueError, "changed during"):
            selection.inspect(
                "runspindle-platform", HEAD, WORKFLOW, "example", changing
            )

    def test_reader_timeout_is_enforced(self) -> None:
        with (
            patch.object(selection, "READ_SECONDS", 0.05),
            self.assertRaises(TimeoutError),
        ):
            selection.read_command(
                [sys.executable, "-c", "import time; time.sleep(20)"]
            )

    def test_no_import_of_effectful_fleet_controller(self) -> None:
        source = Path(selection.__file__).read_text()
        self.assertNotIn("from .backend", source)
        self.assertNotIn("from .manager", source)
        self.assertNotIn("from .state", source)


if __name__ == "__main__":
    unittest.main()
