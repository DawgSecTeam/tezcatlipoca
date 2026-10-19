"""Scoring reads: phantom mid-round polling, and counting blue inject deliverables."""

import importlib.util
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
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
    # /api/services Last10Rounds is newest-first; mid-round the head round has an
    # EMPTY Checks — in flight, not a verdict.

    def test_in_flight_empty_round_falls_back_to_the_last_completed_one(self):
        payload = [{"ServiceName": "web01-nginx", "Last10Rounds": [
            _round([]),
            _round([_check(True)]),
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
        # unmeasured is not up: fail closed, like every gate here
        payload = [{"ServiceName": "db01-sql", "Last10Rounds": [_round([]), _round([])]}]
        self.assertFalse(quotient_api.services_to_rows(payload)[0]["up"])



    def test_a_multi_check_round_needs_every_check_to_pass(self):
        payload = [{"ServiceName": "web01-nginx",
                    "Last10Rounds": [_round([_check(True), _check(False)])]}]
        self.assertFalse(quotient_api.services_to_rows(payload)[0]["up"])


class InjectCounting(unittest.TestCase):
    # teams write sub.md/sub7.md at the team-dir root, not only sub-*.md under submissions/

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
        d = self._team("sub.md", "sub7.md", "sub8.md", "sub9.md", "sub10.md",
                       "sub11.md", "sub12.md")
        self.assertEqual(blue_side.count_inject_submissions(d), 7)



    def test_submissions_directory_still_counts_whatever_is_in_it(self):
        d = self._team(subdir=("brief.md", "notes.txt"))
        self.assertEqual(blue_side.count_inject_submissions(d), 2)

    def test_unrelated_files_are_not_counted(self):
        d = self._team("submarine.md", "submission-notes.md", "NOTES.md", "sub.md.bak")
        self.assertEqual(blue_side.count_inject_submissions(d), 0)


    def test_the_harness_prompt_writes_where_the_counter_looks(self):
        # the prompt and the counter must agree, or the next run fails the gate the same way
        from unittest import mock
        creds = {"TEAM1_PW": "p", "BOX_USER": "u", "BOX_PASSWORD": "b", "KEY_PATH": "/k",
                 "VM_USER": "sysadmin", "ENGINE_IP": "10.0.0.248", "TEAM1_ID": 221,
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
# Per-run gate calibration: the max_simultaneous_down gate
#     judges against red's own pacing cap, and the injects gate gets out of the way when
#     the comp publishes none.

    def _run_dir(self, pacing=None, injects_published=0, injects_submitted=0,
                 services=None, duration_min=None, inject_opens=None):
        d = Path(tempfile.mkdtemp())
        run = {"t0": 1000.0, "phase": "event"}
        if pacing is not None:
            run["pacing"] = pacing
        if duration_min is not None:
            run["duration_min"] = duration_min
        (d / "run.json").write_text(json.dumps(run))
        ev = d / "evidence"
        (ev / "red").mkdir(parents=True)
        services = services or [{"service": "web01-http", "up": True},
                                {"service": "web01-nginx", "up": True},
                                {"service": "web01-ssh", "up": True}]
        injects = []
        for i in range(injects_published):
            row = {"ID": i, "Submissions": []}
            if inject_opens and i < len(inject_opens):
                row["OpenTime"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(1000.0 + inject_opens[i] * 60))
            injects.append(row)
        (ev / "final-scoreboard.json").write_text(json.dumps({
            "services": {"team1": services},
            "injects": injects}))
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

    def test_max_sim_is_capped_at_the_downable_services(self):
        # 5 scored services, two of them SSH (red's access path): red can hold 3
        # down, so a pacing cap of 4 must not demand an impossible 4th.
        services = [{"service": "web01-http", "up": True},
                    {"service": "db01-sql", "up": True},
                    {"service": "win02-ftp", "up": True},
                    {"service": "web01-ssh", "up": True},
                    {"service": "db01-ssh", "up": True}]
        d = self._run_dir(pacing={"max_concurrent_down_start": 2,
                                  "max_concurrent_down_end": 4},
                          services=services)
        out = render.build_report(d)
        rows = dict((k, (v, t, verdict)) for k, v, t, verdict in self._gates(out))
        self.assertEqual(rows["max_simultaneous_down"][1], ">= 3")

    def test_injects_gate_is_capped_at_the_injects_offered(self):
        # 4 published, but a 20-minute window opens only the T0 one: the gate
        # must ask for 1, not 2.
        d = self._run_dir(injects_published=4, duration_min=20,
                          inject_opens=[0, 55, 65, 90])
        out = render.build_report(d)
        rows = dict((k, (v, t, verdict)) for k, v, t, verdict in self._gates(out))
        self.assertEqual(rows["injects"][1], ">= 1")

    def test_zero_published_injects_is_na_not_a_fail(self):
        d = self._run_dir(injects_published=0)
        out = render.build_report(d)
        rows = dict((k, (v, t, verdict)) for k, v, t, verdict in self._gates(out))
        self.assertEqual(rows["injects"][0], "n/a (comp ships none)")
        self.assertNotIn("injects", [k for k, _, _, v in self._gates(out) if v == "FAIL"])





if __name__ == "__main__":
    unittest.main()
