"""The gate table must read tlaloc v2's event schema, not only v1's.

v2 writes {"kind":"action","effect":"takedown","params":{"ip","service","mode"}}
and batches blue restores under {"kind":"blue_restores","restores":[...]}.
Reading only v1's tactic/target/blue_restore spelling reported a fully working
red team as "takedowns 0, distinct tactics 1, targets none" on a live scrim.
"""

import unittest

from scrim_report.red_side import red_metrics

T0 = 1_000_000.0


def _v2_action(effect, ok=True, ip="192.168.120.2", **params):
    return {"ts": "2026-10-18T19:30:00", "kind": "action", "effect": effect,
            "params": {"ip": ip, **params}, "ok": ok, "detail": ""}


class V2SchemaTest(unittest.TestCase):
    def test_a_v2_takedown_counts_as_a_takedown_and_a_target(self):
        events = [
            _v2_action("spray_access", ip="192.168.120.4"),
            _v2_action("takedown", ip="192.168.120.4", service="nginx", mode="stop"),
            _v2_action("takedown", ok=False, ip="192.168.120.5", service="ssh"),
        ]
        m = red_metrics(events, T0, world={"footholds": {}})
        self.assertEqual(m["takedowns"], 1)
        self.assertEqual(m["distinct_tactics"], ["spray_access", "takedown"])
        # only a successful action counts as a touched target
        self.assertEqual(len(m["targets"]), 1)
        self.assertEqual(m["actions_ok"], 2)
        self.assertEqual(m["actions_failed"], 1)
        self.assertEqual(m["timeline"][0][2:], ("nginx", "stop"))

    def test_block_port_is_not_a_takedown(self):
        # It is firewall tamper, not a service stop: counting it inflates the
        # takedown total and the one-target spread with unknown-service rows.
        events = [
            _v2_action("takedown", ip="192.168.120.4", service="nginx", mode="stop"),
            _v2_action("block_port", ip="192.168.120.4", port=21),
        ]
        m = red_metrics(events, T0, world={"footholds": {}})
        self.assertEqual(m["takedowns"], 1)
        self.assertEqual(m["timeline"][0][2], "nginx")

    def test_a_v2_blue_restores_batch_counts_every_restore(self):
        events = [
            _v2_action("takedown", ip="192.168.120.2", service="ADDS", mode="stop"),
            {"ts": "2026-10-18T19:33:00", "kind": "blue_restores",
             "restores": [{"team": "team1", "service": "dc01-ldap"}]},
        ]
        m = red_metrics(events, T0, world={"footholds": {}})
        self.assertEqual(m["blue_restore_events"], 1)
        self.assertEqual(m["blue_restore_list"][0][1], "team1")


if __name__ == "__main__":
    unittest.main()
