"""The red loop persists event_start; a re-staged range kept up past its duration
exits immediately on restart (live: cde-2026 resume: "could not start
tlaloc.service"). A fresh attempt clears it; a resumed event keeps its start."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from scrim import red_link


class Reset(unittest.TestCase):
    def _run(self, resume):
        seen = []

        def fake_run_tree(argv, **kw):
            seen.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="active", stderr="")

        args = SimpleNamespace(resume_event=resume, red_ip="10.0.0.247",
                               red_vmid=1661, competition="CDE-2026",
                               run_dir="/tmp", ssh_key="/tmp/k", red_tunnel=None)
        with patch.object(red_link, "_red_ssh_ctx", return_value=("t", [], None)), \
             patch.object(red_link, "red_unit", return_value="tlaloc.service"), \
             patch.object(red_link.procs, "run_tree", side_effect=fake_run_tree):
            red_link.start_red_director(args)
        return seen[0]

    def test_a_fresh_attempt_clears_the_event_start(self):
        self.assertIn("event_start", self._run(False))

    def test_a_resumed_event_keeps_its_start(self):
        self.assertNotIn("event_start", self._run(True))


if __name__ == "__main__":
    unittest.main()
