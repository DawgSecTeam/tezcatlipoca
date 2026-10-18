"""Shakedown-5x4 event fixes: inject clocks re-anchored at T0, and every scoreboard/
teardown/report loop scaled past 2 teams (teams 3/4 were invisible to evidence)."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


from scrim import endpoints, inject_sync, scoreboard_monitor
from scrim_report import blue_side, host_labels, loaders, render


class InjectReanchorPlan(unittest.TestCase):
    COMP = [{"title": "ransomware", "open_time": "2026-09-29T05:00:00Z",
             "due_time": "2026-09-29T06:00:00Z", "close_time": "2026-09-29T06:30:00Z"},
            {"title": "exfil", "open_time": "2026-09-29T05:10:00Z",
             "due_time": "2026-09-29T06:10:00Z", "close_time": "2026-09-29T06:40:00Z"}]
    REMOTE = [{"ID": 3, "Title": "ransomware", "InjectFileNames": ["brief.pdf"]},
              {"ID": 4, "Title": "exfil", "InjectFileNames": []},
              {"ID": 5, "Title": "engine-only-inject", "InjectFileNames": []}]

    def test_matching_by_title_keeps_attachments(self):
        updates, missing = inject_sync._inject_reanchor_plan(self.COMP, self.REMOTE)
        self.assertEqual([u[0] for u in updates], ["3", "4"])
        # UpdateInject deletes every attachment not re-listed under keep-files
        self.assertEqual(updates[0][2], ["brief.pdf"])
        self.assertEqual(updates[0][1]["open-time"], "2026-09-29T05:00:00Z")
        self.assertEqual(missing, [])



    def test_offset_order_validated_before_any_post(self):
        args = SimpleNamespace(run_dir="/tmp")
        comp = Path(tempfile.mkdtemp())
        inj = comp / "injects" / "bad" / "inject.json"
        inj.parent.mkdir(parents=True)
        inj.write_text(json.dumps({"title": "bad", "open_offset_min": 90,
                                   "due_offset_min": 60, "close_offset_min": 30}))
        with self.assertRaises(SystemExit):
            inject_sync.reanchor_injects(args, comp, {"ENGINE_IP": "x"}, )




class ReportTeamScaling(unittest.TestCase):

    def test_blue_metrics_counts_team4(self):
        rd = Path(tempfile.mkdtemp())
        for n in (1, 2, 3, 4):
            wd = rd / f"blue-team{n}"
            wd.mkdir()
            (wd / "feed.log").write_text("===== cycle 1 rc=0\n===== cycle 2 rc=0\n")
            (wd / "sub-m1.md").write_text("submission")
        m = blue_side.blue_metrics(rd)
        self.assertEqual(m["cycles_rc0"], 8)
        self.assertEqual(m["injects"], 4)

    def test_down_windows_sees_four_teams(self):
        snaps = [(0, {f"team{i}": {"web01": True} for i in range(1, 5)}),
                 (600, {f"team{i}": {"web01": i != 4} for i in range(1, 5)})]
        down = blue_side.down_windows(snaps)
        self.assertEqual(sorted(down), ["team1", "team2", "team3", "team4"])
        self.assertEqual(down["team4"]["restorations"], 0)
        self.assertEqual(down["team4"]["max_simultaneous_down"], 1)




if __name__ == "__main__":
    unittest.main()
