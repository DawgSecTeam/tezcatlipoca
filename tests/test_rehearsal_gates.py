"""The rehearsal gate table, its recalibrated blue thresholds, and the doc that states them.

`scrim_report/gate_table.py` says it must stay in sync with `docs/rehearsal-gates.md`; these
tests are that sync check plus the behaviour of the recalibrated numbers (campaign:
a blue cycle costs 20-30 min and the two teams serialise on one shared endpoint).
"""

import unittest
from pathlib import Path

from scrim_report import gate_table

REPO = Path(__file__).resolve().parents[1]
DOC = (REPO / "docs" / "rehearsal-gates.md").read_text()

BLUE_METRICS = {"cycles_rc0": 0, "restorations": 0, "injects": 0,
                "notebook_entries": 0, "timeouts": 0}


def _blue(metrics):
    return {row[1]: row for row in gate_table.evaluate({}, {**BLUE_METRICS, **metrics}) if row[0] == "blue"}


class BlueGateThresholds(unittest.TestCase):

    def test_the_machine_table_matches_the_documented_numbers(self):
        self.assertEqual(gate_table.GATES["blue"], [
            ("cycles_rc0", 2, ">="), ("restorations", 2, ">="), ("injects", 2, ">="),
            ("notebook_entries", 10, ">="), ("timeouts", 2, "<=")])
        for row in ("| Cycles rc=0 | ≥ 2 |", "| Restorations | ≥ 2 |",
                    "| Injects | ≥ 2 |", "| Notebook entries | ≥ 10 |",
                    "| Cycle timeouts | ≤ 2 |"):
            self.assertIn(row, DOC)

    def test_the_timeouts_gate_is_an_upper_bound(self):
        self.assertEqual(_blue({"timeouts": 0})["timeouts"][4], "PASS")
        self.assertEqual(_blue({"timeouts": 2})["timeouts"][4], "PASS")
        self.assertEqual(_blue({"timeouts": 3})["timeouts"][4], "FAIL")
        self.assertEqual(gate_table.op_str(2, "<="), "<= 2")

    def test_a_measured_cadence_run_passes_both_cadence_gates(self):
        # on the run that found it: 4 cycles in a 45-minute event
        rows = _blue({"cycles_rc0": 4, "timeouts": 1})
        self.assertEqual(rows["cycles_rc0"][4], "PASS")
        self.assertEqual(rows["timeouts"][4], "PASS")

    def test_the_25_minute_scrim_passes_timeouts_but_fails_engagement(self):
        # on the run that found it: 0 rc=0 cycles, 2 timeouts (a cycle cut at the event deadline)
        rows = _blue({"cycles_rc0": 0, "timeouts": 2})
        self.assertEqual(rows["cycles_rc0"][4], "FAIL")
        self.assertEqual(rows["timeouts"][4], "PASS")

    def test_the_red_gates_are_untouched(self):
        self.assertEqual(gate_table.GATES["red"], [
            ("takedowns", 6, ">="), ("restore_reactions", 3, ">="),
            ("distinct_tactics", 4, ">="), ("windows_footholds", 1, ">="),
            ("max_simultaneous_down", 4, ">="), ("stalls", 0, "=="),
            ("evictions", 1, ">=")])


if __name__ == "__main__":
    unittest.main()
