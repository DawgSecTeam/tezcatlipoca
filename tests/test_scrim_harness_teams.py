"""Shakedown-5x4 event fixes: inject clocks re-anchored at T0, and every scoreboard/
teardown/report loop scaled past 2 teams (teams 3/4 were invisible to evidence)."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[1]


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

    def test_missing_titles_reported_not_matched(self):
        comp = self.COMP + [{"title": "ghost", "open_time": "x", "due_time": "x", "close_time": "x"}]
        updates, missing = inject_sync._inject_reanchor_plan(comp, self.REMOTE)
        self.assertEqual(missing, ["ghost"])
        self.assertEqual(len(updates), 2)

    def test_engine_only_injects_untouched(self):
        updates, _ = inject_sync._inject_reanchor_plan(self.COMP, self.REMOTE)
        self.assertNotIn("5", [u[0] for u in updates])

    def test_offset_order_validated_before_any_post(self):
        args = SimpleNamespace(run_dir="/tmp")
        comp = Path(tempfile.mkdtemp())
        inj = comp / "injects" / "bad" / "inject.json"
        inj.parent.mkdir(parents=True)
        inj.write_text(json.dumps({"title": "bad", "open_offset_min": 90,
                                   "due_offset_min": 60, "close_offset_min": 30}))
        with self.assertRaises(SystemExit):
            inject_sync.reanchor_injects(args, comp, {"ENGINE_IP": "x"}, )


class BlueEndpoints(unittest.TestCase):
    ARGS = SimpleNamespace(blue_base_url="http://shared", blue_model="m",
                           blue2_base_url=None, blue2_model=None,
                           blue3_base_url="http://three", blue3_model=None,
                           blue4_base_url=None, blue4_model=None)

    def test_per_team_endpoint_and_fallback(self):
        self.assertEqual(endpoints.blue_ep(self.ARGS, 3), ("http://three", "m"))
        self.assertEqual(endpoints.blue_ep(self.ARGS, 4), ("http://shared", "m"))
        self.assertEqual(endpoints.blue_ep(self.ARGS, 2), ("http://shared", "m"))

    def test_one_lock_per_distinct_endpoint(self):
        import threading
        locks = {}
        for n in range(1, 5):
            base_url, _ = endpoints.blue_ep(self.ARGS, n)
            locks.setdefault(base_url, threading.Lock())
        self.assertEqual(len(locks), 2)

    def test_cred_team_names_bounded_and_cred_gated(self):
        args = SimpleNamespace(teams=4)
        creds = {"TEAM1_PW": "x", "TEAM2_PW": "x", "TEAM3_PW": "x"}
        self.assertEqual(scoreboard_monitor._cred_team_names(args, creds), ["team1", "team2", "team3"])


class ReportTeamScaling(unittest.TestCase):
    def test_host_label_maps_all_identifiers(self):
        self.assertTrue(host_labels.host_label("192.168.101.4").startswith("1:"))
        self.assertTrue(host_labels.host_label("192.168.103.6").startswith("3:"))
        self.assertTrue(host_labels.host_label("192.168.104.6").startswith("4:"))
        # non-team addresses stay unlabeled
        self.assertNotIn(":", host_labels.host_label("10.0.0.198"))

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

    def test_final_scoreboard_section_renders_from_evidence(self):
        rd = Path(tempfile.mkdtemp())
        ev = rd / "evidence"
        ev.mkdir()
        (ev / "final-scoreboard.json").write_text(json.dumps({
            "captured_at": "2026-09-29T07:32:00",
            "teams": [{"ID": 1, "Name": "team1"}],
            "injects": [{"ID": 3, "Title": "ransomware",
                         "Submissions": [{"ID": 1}, {"ID": 2}]}],
            "services": {"team1": [{"service": "web01", "up": True, "error": ""},
                                   {"service": "db01", "up": False, "error": "refused"}],
                         "team4": None},
        }))
        self.assertIsNotNone(loaders.load_final_scoreboard(rd))
        out, _meta = render.build_report(rd)
        self.assertIn("Final scores (evidence dump at capture, before teardown)", out)
        self.assertIn("team1: 1 up / 1 down", out)
        self.assertIn("2 submissions", out)

    def test_no_final_scoreboard_no_section(self):
        rd = Path(tempfile.mkdtemp())
        out, _meta = render.build_report(rd)
        self.assertNotIn("Final scores", out)


if __name__ == "__main__":
    unittest.main()
