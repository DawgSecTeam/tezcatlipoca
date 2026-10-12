"""Blue cycles must complete, not pace to a 30-minute budget.

run3 (2026-10-11): CYCLE_TIMEOUT 1800 + a prompt offering "~25 wall-clock
minutes" let each opencode cycle run the entire 30-minute budget — the whole
60-minute event was two TIMED OUT cycles (cycles_rc0 0 of >= 8, timeouts 2 of
== 0). The budgets and the prompt scope are pinned together: the cap only works
if the prompt asks for a cycle that fits inside it.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrim import core  # noqa: E402


class CycleBudget(unittest.TestCase):
    def test_cycle_fits_inside_the_target_period(self):
        self.assertLess(core.CYCLE_TIMEOUT, 900,
                        "a cycle cap near 30 min collapses the event to 2 cycles")
        self.assertLess(core.CYCLE_TIMEOUT, core.CYCLE_TARGET_PERIOD + 240,
                        "cap must sit near the target period, not 3x above it")

    def test_target_period_allows_the_gate_minimum(self):
        # cycles rc=0 gate wants >= 8 in a 60-minute event: a cycle that takes
        # its whole target period must still yield 7+ starts.
        self.assertLessEqual(core.CYCLE_TARGET_PERIOD, 480)

    def test_board_sampling_catches_a_fast_blue(self):
        # restorations graded from DOWN->UP between samples; live blue restored
        # in 2-5 min, so 300 s sampling missed every transition.
        self.assertLessEqual(core.MONITOR_INTERVAL, 60)

    def test_prompt_scopes_the_cycle_inside_the_cap(self):
        from scrim import blue_prompt
        import inspect
        src = inspect.getsource(blue_prompt)
        self.assertNotIn("25 wall-clock minutes", src)
        self.assertIn("6 wall-clock minutes", src)
        self.assertIn("EXACTLY ONE change/fix action", src)
        self.assertIn("blue-watchdog", src)          # restores are not cycle work
        self.assertIn("end your reply", src)


if __name__ == "__main__":
    unittest.main()
