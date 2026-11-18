"""Verify-only network preflight never repairs its synthetic host observations."""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from runner_fleet import host_helper as h


def fixture():
    rows = {}
    for name, address, index, peer in [
        ("rsvm0", "10.231.0.1", 1, None),
        ("rsvm1", "10.231.1.1", 2, None),
        ("rscan0", "198.18.231.1", 3, 4),
        ("rscan1", "198.18.231.2", 4, 3),
    ]:
        link = (
            {"info_kind": "veth"}
            if peer
            else {
                "info_kind": "tun",
                "info_data": {
                    "type": "tap",
                    "user": "runspindle-vm" + name[-1],
                    "persist": True,
                    "multi_queue": False,
                },
            }
        )
        rows[name] = {
            "ifname": name,
            "ifindex": index,
            "link_index": peer,
            "flags": ["UP"],
            "linkinfo": link,
            "addr_info": [
                {"family": "inet", "local": address, "prefixlen": 30, "scope": "global"}
            ],
        }
    return rows


class VerificationTests(unittest.TestCase):
    def verify(self, rows=None, table=None, active="active", routes=None):
        rows = fixture() if rows is None else rows
        calls = []

        def run(*argv):
            calls.append(argv)
            if argv == (
                "nft",
                "-s",
                "-nn",
                "list",
                "table",
                "inet",
                "runspindle_fleet",
            ):
                return SimpleNamespace(
                    stdout=h.expected_network_table() if table is None else table
                )
            if argv[0] == "ip" and argv[-4:-1] == ("addr", "show", "dev"):
                return SimpleNamespace(stdout=json.dumps([rows[argv[-1]]]))
            if argv == ("ip", "-j", "-n", "runspindle-canary", "route", "show"):
                value = (
                    routes
                    if routes is not None
                    else [
                        {
                            "dst": "default",
                            "gateway": "198.18.231.1",
                            "dev": "rscan1",
                            "flags": [],
                        },
                        {
                            "dst": "198.18.231.0/30",
                            "dev": "rscan1",
                            "protocol": "kernel",
                            "scope": "link",
                            "prefsrc": "198.18.231.2",
                            "flags": [],
                        },
                    ]
                )
                return SimpleNamespace(stdout=json.dumps(value))
            if argv[0:2] == ("systemctl", "is-active"):
                return SimpleNamespace(stdout=active)
            self.fail(f"unexpected or mutating command: {argv}")

        with patch.object(h, "run", side_effect=run):
            result = h.verify_network()
        self.assertEqual(len(calls), 10)
        return result

    def test_valid_observation_only(self):
        self.assertEqual(self.verify()["status"], "verified")

    def test_firewall_missing_or_extra_rule(self):
        for table in [
            "",
            h.expected_network_table().replace("counter drop", "counter accept", 1),
            h.expected_network_table() + " extra",
        ]:
            with self.subTest(table=table), self.assertRaises(RuntimeError):
                self.verify(table=table)

    def test_tap_owner_type_bridge_address_or_down(self):
        for mutate in [
            lambda r: r["linkinfo"]["info_data"].update(user="other"),
            lambda r: r["linkinfo"]["info_data"].update(type="tun"),
            lambda r: r.update(master="bridge0"),
            lambda r: r.update(flags=[]),
            lambda r: r["addr_info"][0].update(prefixlen=24),
            lambda r: r["addr_info"].append({"family": "inet", "local": "1.2.3.4"}),
        ]:
            rows = fixture()
            mutate(rows["rsvm0"])
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                self.verify(rows=rows)

    def test_peer_and_routes_drift(self):
        rows = fixture()
        rows["rscan0"]["link_index"] = 999
        with self.assertRaises(RuntimeError):
            self.verify(rows=rows)
        with self.assertRaises(RuntimeError):
            self.verify(routes=[])

    def test_inactive_service_never_started(self):
        with self.assertRaises(RuntimeError):
            self.verify(active="inactive")


if __name__ == "__main__":
    unittest.main()
