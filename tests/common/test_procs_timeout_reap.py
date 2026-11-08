"""run_tree's timeout path must reap a killed child and raise ScrimTimeout.

Python 3.13 clears Popen's pipe bookkeeping when communicate() times out, so the
documented "call communicate() again after killing" recipe raises AttributeError
and replaces the real timeout — and the command that timed out — with a crash
(live: run-700f7719, the deploy's stage_verify). Offline: a real short-lived child."""

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common import procs  # noqa: E402


class TimeoutReap(unittest.TestCase):

    def test_a_timeout_raises_scrim_timeout_not_an_attribute_error(self):
        with self.assertRaises(procs.ScrimTimeout):
            procs.run_tree(["bash", "-c", "sleep 30"], timeout=1, grace=1)

    def test_a_fast_command_still_returns_its_output(self):
        r = procs.run_tree(["bash", "-c", "echo hello"], timeout=30)
        self.assertEqual(r.stdout.strip(), "hello")
        self.assertEqual(r.returncode, 0)
