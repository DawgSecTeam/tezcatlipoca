"""Boundary behaviour of the blue rehearsal gates (meaning and calibration: docs/rehearsal-gates.md)."""

import unittest

from scrim_report import gate_table

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


if __name__ == "__main__":
    unittest.main()
