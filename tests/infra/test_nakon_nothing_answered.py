"""strict=False still fails when nothing answered: a sweep that ran zero steps raises."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from nakonops import nakon_run_ops  # noqa: E402


class _FakeProc:
    def __init__(self, line):
        self._line = line
        self.returncode = 0

    @property
    def stdout(self):
        return iter([self._line + "\n"])

    def wait(self, timeout=None):
        return self.returncode


def _run_with(json_line, strict=False):
    def fake_run(*a, **kw):
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def fake_popen(*a, **kw):
        return _FakeProc(json_line)

    with patch.object(nakon_run_ops.subprocess, "run", side_effect=fake_run), \
            patch.object(nakon_run_ops.subprocess, "Popen", side_effect=fake_popen), \
            patch.object(nakon_run_ops.time, "sleep"):
        return nakon_run_ops.run_nakon(
            key="/k", scoring_user="ubuntu", scoring_ip="10.0.0.248",
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
        with self.assertRaises(RuntimeError) as raised:
            _run_with(_machines([], [], []))
        msg = str(raised.exception)
        self.assertIn("nothing was applied", msg)
        self.assertIn("3 machine(s) ran zero steps", msg)

    def test_one_machine_that_ran_anything_is_tolerated(self):
        # a partial sweep is a real outcome; escalating it defeats strict=False
        result = _run_with(_machines([{"index": "000", "name": "ADDS", "rc": 1}], []))
        self.assertEqual(len(result.machines), 2)

    def test_result_glued_to_status_line_is_still_parsed(self):
        # nakon can emit the JSON glued onto its status line; it must still parse
        glued = ("[nakon] All configurations completed with exit 0."
                 + _machines([{"index": "000", "name": "nginx", "rc": 0}]))
        result = _run_with(glued)
        self.assertEqual(result.machines[0]["name"], "box1")


if __name__ == "__main__":
    unittest.main()
