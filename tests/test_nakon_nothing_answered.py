"""strict=False still fails when nothing answered (C2) — scale8 soak 2026-10-02.

The phase-5 repair sweep runs `nakon deploy` with strict=False so a couple of flaky pins
of 41 cannot abort a deploy. But a host that never answered cannot report a failing step,
so an all-unreachable sweep finishes rc=0 with no FAILED lines — and that is exactly what
happened: the sweep "succeeded" against 32 machines that did not exist, the caller
checkpointed phase 5, and the next resume chased ghosts for ~40 minutes.

The distinction the floor must draw:
    2 flaky steps of 41   -> tolerated (that is what strict=False is FOR)
    41 of 41 silent       -> raise    (the sweep shot at nothing)

Driven through the real `run_nakon` (only the subprocess boundary is faked), so the argv
it builds and the rc/JSON handling it does are the ones that ship.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import nakon_ops  # noqa: E402


class _FakeProc:
    """A Popen stand-in whose stdout yields one line, then EOF."""

    def __init__(self, line):
        self._line = line
        self.returncode = 0

    @property
    def stdout(self):
        return iter([self._line + "\n"])

    def wait(self, timeout=None):
        return self.returncode


def _run_with(json_line, strict=False):
    """Invoke run_nakon with the ssh/scp boundary faked; return the NakonResult or raise."""
    def fake_run(*a, **kw):
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def fake_popen(*a, **kw):
        return _FakeProc(json_line)

    with patch.object(nakon_ops.subprocess, "run", side_effect=fake_run), \
            patch.object(nakon_ops.subprocess, "Popen", side_effect=fake_popen), \
            patch.object(nakon_ops, "_deploy_owner_check", return_value="me@host"), \
            patch.object(nakon_ops.time, "sleep"):
        return nakon_ops.run_nakon(
            key="/k", scoring_user="ubuntu", scoring_ip="10.0.0.252",
            bundle=Path("/tmp/b.tar.gz"), config_path=Path("/tmp/c.json"),
            strict=strict, run_tag="t")


def _machines(*steps_per_machine):
    return json.dumps({"machines": [
        {"name": f"box{i}", "ip": f"192.168.2{i}.2", "error": None,
         "exit_status": 0 if steps else 255, "steps": steps, "failures": []}
        for i, steps in enumerate(steps_per_machine, start=1)
    ]})


class NothingAnsweredFloor(unittest.TestCase):
    def test_all_machines_silent_raises_even_with_rc0(self):
        # The soak's shape: every machine unreachable, nakon rc=0, zero FAILED steps.
        with self.assertRaises(RuntimeError) as raised:
            _run_with(_machines([], [], []))
        msg = str(raised.exception)
        self.assertIn("nothing was applied", msg)
        self.assertIn("3 machine(s) ran zero steps", msg)

    def test_missing_machine_results_raises(self):
        # Older nakon never emits the JSON at all: rc=0, no FAILED lines, no evidence
        # anything ran. Unverifiable is not the same as fine.
        with self.assertRaises(RuntimeError) as raised:
            _run_with("no json here")
        self.assertIn("no per-machine results", str(raised.exception))

    def test_one_machine_that_ran_anything_is_tolerated(self):
        # A partial sweep is a real outcome, not a broken one — escalating it would
        # abort runs that are merely imperfect, which is what strict=False is for.
        result = _run_with(_machines([{"index": "000", "name": "ADDS", "rc": 1}], []))
        self.assertEqual(len(result.machines), 2)

    def test_a_clean_sweep_still_returns(self):
        result = _run_with(_machines([{"index": "000", "name": "ADDS", "rc": 0}]))
        self.assertEqual(result.machines[0]["steps"][0]["rc"], 0)

    def test_strict_true_is_not_affected(self):
        # The floor is about "nothing answered", not about strictness: a strict run that
        # genuinely reached its hosts must behave exactly as before.
        result = _run_with(_machines([{"index": "000", "name": "x", "rc": 0}]), strict=True)
        self.assertEqual(len(result.machines), 1)


class FloorPredicate(unittest.TestCase):
    """The predicate itself, including the boundary it must NOT cross."""

    def test_empty_list_is_nothing_answered(self):
        self.assertIn("no per-machine results", nakon_ops._nothing_answered([]))

    def test_all_zero_step_machines_is_nothing_answered(self):
        self.assertIn("2 machine(s)",
                      nakon_ops._nothing_answered([{"name": "a", "steps": []},
                                                   {"name": "b", "steps": []}]))

    def test_one_step_anywhere_clears_the_floor(self):
        self.assertIsNone(nakon_ops._nothing_answered([{"name": "a", "steps": []},
                                                       {"name": "b", "steps": [{"rc": 1}]}]))

    def test_a_failing_step_is_still_a_step(self):
        self.assertIsNone(nakon_ops._nothing_answered([{"name": "a", "steps": [{"rc": 1}]}]))


if __name__ == "__main__":
    unittest.main()
