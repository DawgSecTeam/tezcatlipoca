"""Boundary behaviour of the red and blue rehearsal gates (meaning and calibration:
docs/rehearsal-gates.md)."""

import unittest

from scrim_report import gate_table
from scrim_report.red_side import red_metrics

BLUE_METRICS = {"cycles_rc0": 0, "restorations": 0, "injects": 0,
                "notebook_entries": 0, "timeouts": 0}


def _blue(metrics):
    return {row[1]: row for row in gate_table.evaluate({}, {**BLUE_METRICS, **metrics}) if row[0] == "blue"}


class BlueGateThresholds(unittest.TestCase):

    def test_the_timeouts_gate_is_an_upper_bound(self):
        self.assertEqual(_blue({"timeouts": 0})["timeouts"][4], "PASS")
        self.assertEqual(_blue({"timeouts": 2})["timeouts"][4], "PASS")
        self.assertEqual(_blue({"timeouts": 3})["timeouts"][4], "FAIL")
        self.assertEqual(gate_table.op_str(2, "<="), "<= 2")

    def test_the_25_minute_scrim_passes_timeouts_but_fails_engagement(self):
        # 0 rc=0 cycles with no CAP cut: a cycle cut by the event window is reported as
        # window_cuts and is not a blue failure (docs/rehearsal-gates.md).
        rows = _blue({"cycles_rc0": 0, "timeouts": 0})
        self.assertEqual(rows["cycles_rc0"][4], "FAIL")
        self.assertEqual(rows["timeouts"][4], "PASS")


class RedBalanceGate(unittest.TestCase):
    # run-84c0e45d ended 37 takedowns on team2 and 3 on team1: the aggregate gate was green
    # and nothing said one blue team had spent the second hour in silence.

    TEAMS = {"127": "team1", "128": "team2"}

    def _takedowns(self, counts):
        events = []
        for octet, n in counts.items():
            events += [{"ts": "2026-11-17T05:00:00", "kind": "action", "effect": "takedown",
                        "params": {"ip": f"192.168.{octet}.10", "service": "nginx",
                                   "mode": "stop"},
                        "ok": True, "detail": ""} for _ in range(n)]
        return red_metrics(events, 1_000_000.0, {"footholds": {}}, teams=self.TEAMS)

    def _rows(self, rm):
        return {r[1]: r for r in gate_table.evaluate(
            {"takedowns": rm["takedowns"], "min_team_takedowns": rm["min_team_takedowns"]},
            {}, thresholds={"min_team_takedowns": max(
                1, int(rm["takedowns"] / (2 * len(rm["takedowns_by_team"]))))})
            if r[0] == "red"}

    def test_a_37_to_3_sweep_fails_balance_while_aggregate_passes(self):
        rm = self._takedowns({"128": 37, "127": 3})
        self.assertEqual(rm["takedowns_by_team"], {"team1": 3, "team2": 37})
        rows = self._rows(rm)
        self.assertEqual(rows["takedowns"][4], "PASS")     # the hole this gate exists to plug
        self.assertEqual(rows["min_team_takedowns"][4], "FAIL")

    def test_a_fair_split_passes(self):
        rm = self._takedowns({"128": 20, "127": 20})
        self.assertEqual(self._rows(rm)["min_team_takedowns"][4], "PASS")

    def test_a_starved_team_of_one_is_the_static_floor_case(self):
        # small run: 1 to 0 still fails — the untouched team is the whole point
        rm = self._takedowns({"128": 1})
        self.assertEqual(self._rows(rm)["min_team_takedowns"][4], "FAIL")

    def test_without_team_attribution_the_gate_says_n_a_not_a_lie(self):
        m = red_metrics([{"ts": "2026-11-17T05:00:00", "kind": "action", "effect": "takedown",
                          "params": {"ip": "192.168.128.10", "service": "n", "mode": "stop"},
                          "ok": True, "detail": ""}], 1_000_000.0, {"footholds": {}})
        self.assertIsNone(m["min_team_takedowns"])
        row = [r for r in gate_table.evaluate({"min_team_takedowns": None}, {})
               if r[1] == "min_team_takedowns"][0]
        self.assertEqual(row[4], "n/a")


if __name__ == "__main__":
    unittest.main()
