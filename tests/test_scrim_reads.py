"""Scoring reads: the phantom round (E3) and the inject-count path (E4).

Two harness-side reading defects from scale8-soak-2026-10-02, both of which made the
automated evidence disagree with what actually happened:

E3 — `/api/services` returns `Last10Rounds` newest-first, and at >=8 teams a poll lands
mid-round: rounds[0] is then the round in flight with an EMPTY `Checks` array, which is
not a verdict. Reading it as "no check passed" invents a DOWN that the monitors, the
fire-test and every scoreboard snapshot then believe.

E4 — `INTERACTION.md` reported "injects submitted: 0" and failed the blue inject gate for
a run that had submitted them: the teams wrote `sub.md`, `sub7.md` … `sub12.md` at the
team directory root and the counter only looked for `sub-*.md` or files under
`submissions/`. A gate that reports zero because of a filename fails a team for work it
did.
"""

import importlib.util
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


from scrim import blue_prompt, compworld, quotient_api
from scrim_report import blue_side, render


def _round(checks, start="2026-10-02T06:36:00Z"):
    return {"ID": 1, "StartTime": start, "Checks": checks, "SLAs": None}


def _check(result, error=""):
    return {"TeamID": 1, "RoundID": 224, "ServiceName": "app01-dns", "Points": 5,
            "Result": result, "Error": error, "Debug": ""}


class PhantomRound(unittest.TestCase):
    """E3: an in-flight round must not read as a down service."""

    def test_in_flight_empty_round_falls_back_to_the_last_completed_one(self):
        payload = [{"ServiceName": "web01-nginx", "Last10Rounds": [
            _round([]),                      # in flight — not a verdict
            _round([_check(True)]),          # last completed — UP
        ]}]
        rows = quotient_api.services_to_rows(payload)
        self.assertTrue(rows[0]["up"], "an in-flight round must not invent a DOWN")

    def test_a_real_down_in_the_last_completed_round_still_reads_down(self):
        payload = [{"ServiceName": "app01-dns", "Last10Rounds": [
            _round([]),
            _round([_check(False, "no records received")]),
        ]}]
        rows = quotient_api.services_to_rows(payload)
        self.assertFalse(rows[0]["up"])
        self.assertIn("no records received", rows[0]["error"])

    def test_no_round_with_checks_stays_down(self):
        # Unmeasured is not up. Fail closed, exactly like every other gate in this repo.
        payload = [{"ServiceName": "db01-sql", "Last10Rounds": [_round([]), _round([])]}]
        self.assertFalse(quotient_api.services_to_rows(payload)[0]["up"])

    def test_empty_and_missing_round_lists_do_not_crash(self):
        payload = [{"ServiceName": "a", "Last10Rounds": []},
                   {"ServiceName": "b"},
                   {"ServiceName": "c", "Last10Rounds": None}]
        rows = quotient_api.services_to_rows(payload)
        self.assertEqual([r["service"] for r in rows], ["a", "b", "c"])
        self.assertFalse(any(r["up"] for r in rows))

    def test_null_body_is_still_a_legitimate_empty_team(self):
        self.assertEqual(quotient_api.services_to_rows(None), [])

    def test_a_multi_check_round_needs_every_check_to_pass(self):
        payload = [{"ServiceName": "web01-nginx",
                    "Last10Rounds": [_round([_check(True), _check(False)])]}]
        self.assertFalse(quotient_api.services_to_rows(payload)[0]["up"])


class InjectCounting(unittest.TestCase):
    """E4: count the deliverables where the agents actually write them."""

    def _team(self, *files, subdir=()):
        d = Path(tempfile.mkdtemp())
        for name in files:
            (d / name).write_text("deliverable")
        if subdir:
            (d / "submissions").mkdir()
            for name in subdir:
                (d / "submissions" / name).write_text("deliverable")
        return d

    def test_soak_shapes_are_counted(self):
        # The exact filenames the soak's teams produced.
        d = self._team("sub.md", "sub7.md", "sub8.md", "sub9.md", "sub10.md",
                       "sub11.md", "sub12.md")
        self.assertEqual(blue_side.count_inject_submissions(d), 7)

    def test_documented_hyphenated_form_is_counted(self):
        d = self._team("sub-3.md", "sub-4.txt")
        self.assertEqual(blue_side.count_inject_submissions(d), 2)

    def test_non_numeric_inject_id_is_counted(self):
        d = self._team("sub-m1.md")
        self.assertEqual(blue_side.count_inject_submissions(d), 1)

    def test_submissions_directory_still_counts_whatever_is_in_it(self):
        d = self._team(subdir=("brief.md", "notes.txt"))
        self.assertEqual(blue_side.count_inject_submissions(d), 2)

    def test_unrelated_files_are_not_counted(self):
        # A report that over-counts is as useless as one that under-counts.
        d = self._team("submarine.md", "submission-notes.md", "NOTES.md", "sub.md.bak")
        self.assertEqual(blue_side.count_inject_submissions(d), 0)

    def test_blue_metrics_adds_the_count_across_teams(self):
        rd = Path(tempfile.mkdtemp())
        for n in (1, 2, 3, 4):
            wd = rd / f"blue-team{n}"
            wd.mkdir()
            (wd / "feed.log").write_text("===== cycle 1 rc=0\n")
            # Two teams follow the old prompt, two the new one.
            if n <= 2:
                (wd / "sub.md").write_text("x")
                (wd / "sub7.md").write_text("x")
            else:
                (wd / "submissions").mkdir()
                (wd / "submissions" / "sub-1.md").write_text("x")
        self.assertEqual(blue_side.blue_metrics(rd)["injects"], 6)

    def test_the_harness_prompt_writes_where_the_counter_looks(self):
        # The two halves must agree, or the next run repeats the soak exactly.
        from unittest import mock
        creds = {"TEAM1_PW": "p", "BOX_USER": "u", "BOX_PASSWORD": "b", "KEY_PATH": "/k",
                 "VM_USER": "sysadmin", "ENGINE_IP": "10.0.0.252", "TEAM1_ID": 221,
                 "ADMIN_PW": "a"}
        args = type("A", (), {"teams": 1, "competition": "c", "duration_min": 120,
                              "run_dir": "/tmp"})()
        world = {"linux": ["web01", "app01"], "windows": [], "web_unit": "nginx",
                 "name": "probe", "scenario": ""}
        with mock.patch.object(compworld, "comp_world", return_value=world):
            prompt = blue_prompt.blue_cycle_prompt(1, creds, args, 0, 120, "", "", "", "")
        self.assertIn("submissions/", prompt)
        self.assertNotIn("> sub.md", prompt)


class GateCalibration(unittest.TestCase):
    """Per-run gate calibration (2026-10-04 hardening): the max_simultaneous_down gate
    judges against red's own pacing cap, and the injects gate gets out of the way when
    the comp publishes none."""

    def _run_dir(self, pacing=None, injects_published=0, injects_submitted=0):
        d = Path(tempfile.mkdtemp())
        run = {"t0": 1000.0, "phase": "event"}
        if pacing is not None:
            run["pacing"] = pacing
        (d / "run.json").write_text(json.dumps(run))
        ev = d / "evidence"
        (ev / "red").mkdir(parents=True)
        (ev / "final-scoreboard.json").write_text(json.dumps({
            "services": {"team1": [{"service": "web01-http", "up": True}]},
            "injects": [{"ID": i, "Submissions": []}
                        for i in range(injects_published)]}))
        wd = d / "blue-team1"
        wd.mkdir(exist_ok=True)
        (wd / "feed.log").write_text("===== cycle 1 rc=0\n")
        for n in range(injects_submitted):
            (wd / f"sub-{n}.md").write_text("deliverable")
        return d

    def _gates(self, out):
        rows = []
        for line in (out[0] if isinstance(out, tuple) else out).splitlines():
            m = re.match(r"\| (\w+) \| (\w+) \| ([^|]*) \| ([^|]*) \| ([^|]+?) \s*\|$", line)
            if m:
                rows.append((m.group(2), m.group(3).strip(), m.group(4).strip(), m.group(5)))
        return rows

    def test_max_sim_judges_against_the_pacing_cap(self):
        d = self._run_dir(pacing={"max_concurrent_down_start": 2,
                                  "max_concurrent_down_end": 2})
        out = render.build_report(d)
        rows = dict((k, (v, t, verdict)) for k, v, t, verdict in self._gates(out))
        self.assertEqual(rows["max_simultaneous_down"][1], ">= 2",
                         "the gate must judge against the cap red was actually given")

    def test_legacy_run_dirs_keep_the_flat_four(self):
        d = self._run_dir(pacing=None)
        out = render.build_report(d)
        rows = dict((k, (v, t, verdict)) for k, v, t, verdict in self._gates(out))
        self.assertEqual(rows["max_simultaneous_down"][1], ">= 4")

    def test_zero_published_injects_is_na_not_a_fail(self):
        d = self._run_dir(injects_published=0)
        out = render.build_report(d)
        rows = dict((k, (v, t, verdict)) for k, v, t, verdict in self._gates(out))
        self.assertEqual(rows["injects"][0], "n/a (comp ships none)")
        self.assertNotIn("injects", [k for k, _, _, v in self._gates(out) if v == "FAIL"])

    def test_submissions_above_published_still_count(self):
        d = self._run_dir(injects_published=3, injects_submitted=2)
        out = render.build_report(d)
        rows = dict((k, (v, t, verdict)) for k, v, t, verdict in self._gates(out))
        self.assertEqual(rows["injects"][0], "2")
        self.assertEqual(rows["injects"][2], "PASS")

    def test_spread_metric_names_the_dominant_target(self):
        d = self._run_dir()
        ev = d / "evidence" / "red"
        # 4 takedowns: 3 on one host, 1 on another (run.json t0 anchors T+)
        lines = []
        for i, ip in enumerate(["192.168.130.5", "192.168.130.5", "192.168.130.5",
                                "192.168.130.4"]):
            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1000 + 60 * (i + 1)))
            lines.append(json.dumps({
                "ts": ts, "kind": "action", "tactic": "impact_service", "ok": True,
                "target": ip,
                "detail": f"svc is DOWN on {ip} (stop_disable)",
                "data": {"unit": "svc", "mode": "stop_disable"}}))
        (ev / "events.jsonl").write_text("\n".join(lines) + "\n")
        (d / "evidence" / "red" / "world.json").write_text(json.dumps({"meta": {}}))
        out = render.build_report(d)
        self.assertRegex(out[0] if isinstance(out, tuple) else out,
                         r"takedown spread: \*\*75%\*\* on one target")



if __name__ == "__main__":
    unittest.main()
