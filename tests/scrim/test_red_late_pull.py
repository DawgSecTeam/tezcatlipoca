"""The teardown-time red pull is a safety net, never a re-fetch of hashed evidence.

Caught live (run-4c4a9507-run2): stage_teardown pulled red state, collected it, wrote the
report, and then teardown_red pulled AGAIN — overwriting evidence/red/events.jsonl and
world.json after the collection recorded their hashes, so the seal reported hash drift on
files nobody had touched. The second pull still has to land a late report (or everything,
when the first pull failed), which is why it skips only what is already on disk.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scrim import red_link


class LatePull(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.args = SimpleNamespace(run_dir=self.tmp.name, competition="mini",
                                    comp_dir=None, red_ip="10.0.0.198",
                                    red_tunnel=None, test_dir=None, resume_event=False,
                                    red_vmid=999, ssh_key="/tmp/k")
        self.copied = []

    def _run(self, only_missing, staged):
        def fake_run_tree(argv, **_kw):
            if argv[0] == "scp":
                dest = Path(argv[-1])
                if dest.exists() and only_missing:
                    raise AssertionError(f"re-fetched {dest.name} after collection")
                dest.write_text("new\n")
                self.copied.append(dest.name)
            return SimpleNamespace(returncode=0, stdout=staged, stderr="")

        with patch.object(red_link, "_red_ssh_ctx", return_value=("t", [], None)), \
             patch.object(red_link, "await_red_report", return_value=True), \
             patch.object(red_link, "_staged_report_names", return_value=[]), \
             patch.object(red_link.procs, "run_tree", side_effect=fake_run_tree):
            return red_link.pull_red_state(self.args, only_missing=only_missing)

    def test_the_second_pull_keeps_already_collected_files(self):
        ev = Path(self.tmp.name) / "evidence" / "red"
        ev.mkdir(parents=True)
        (ev / "events.jsonl").write_text("collected\n")
        self._run(only_missing=True, staged="")
        self.assertEqual((ev / "events.jsonl").read_text(), "collected\n")
        self.assertNotIn("events.jsonl", self.copied)
        self.assertIn("world.json", self.copied)      # missing files still land

    def test_a_missing_file_still_lands_when_the_first_pull_failed(self):
        got = self._run(only_missing=True, staged="")
        self.assertIn("world.json", got)
