"""remote_access_ops: policy/firewall renderers (golden), roster validation, and the
headscale IO paths against a patched _hs_ssh/hs_json — all offline."""

import json
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import remote_access_ops as ra  # noqa: E402


COMPS_ONE = [{"comp_id": "cde-2026", "engine_ip": "10.0.0.252",
              "teams": [{"identifier": "101", "participants": ["alice", "bob"]},
                        {"identifier": "102", "participants": []}]}]

COMPS_TWO = [{"comp_id": "cde-2026", "engine_ip": "10.0.0.252",
              "teams": [{"identifier": "101", "participants": ["alice"]}]},
             {"comp_id": "scrim-two", "engine_ip": "10.0.0.250",
              "teams": [{"identifier": "130", "participants": ["carol"]}]}]

FULL_ACCESS = ["cyberrange-infra@", "hnasher1@", "dipam1@", "sdavis24@", "ckegly@"]


class RenderPolicyTest(unittest.TestCase):
    def test_foundation_only(self):
        text = ra.render_policy(FULL_ACCESS, [])
        self.assertIn('"group:full-access": ["cyberrange-infra@", "hnasher1@", '
                      '"dipam1@", "sdavis24@", "ckegly@",]', text)
        self.assertIn('"tag:range-router": ["cyberrange-infra@"],', text)
        self.assertIn('"192.168.0.0/16": ["tag:range-router"]', text)
        self.assertEqual(text.count('"action": "accept"'), 1)

    def test_participant_team_gets_scoped_acl(self):
        text = ra.render_policy(FULL_ACCESS, COMPS_ONE)
        self.assertIn('"tag:comp-cde-2026-team-101"', text)  # tagOwners entry
        self.assertIn('{"action": "accept", "src": ["tag:comp-cde-2026-team-101"], '
                      '"dst": ["192.168.101.0/24:*", "10.0.0.252:80"]},', text)
        # team 102 has no participants: no ACL line, no tagOwners entry
        self.assertNotIn("comp-cde-2026-team-102", text)
        self.assertEqual(text.count('"action": "accept"'), 2)

    def test_two_comps_deterministic_and_scoped(self):
        a = ra.render_policy(FULL_ACCESS, COMPS_TWO)
        b = ra.render_policy(FULL_ACCESS, list(reversed(COMPS_TWO)))
        self.assertEqual(a, b)  # sorted by comp id: output order is canonical
        self.assertIn("tag:comp-scrim-two-team-130", a)
        self.assertIn('"192.168.130.0/24:*", "10.0.0.250:80"', a)

    def test_engine_ip_without_prefix(self):
        comps = [{"comp_id": "x", "engine_ip": "10.0.0.7",
                  "teams": [{"identifier": "101", "participants": ["a"]}]}]
        self.assertIn('"10.0.0.7:80"', ra.render_policy(FULL_ACCESS, comps))

    def test_valid_hujson_up_to_comments(self):
        # strip // comments and trailing commas -> must parse as strict JSON
        text = ra.render_policy(FULL_ACCESS, COMPS_TWO)
        lines = [ln.split("//")[0].rstrip() for ln in text.splitlines()]
        cleaned = re.sub(r",\s*([}\]])", r"\1", "\n".join(lines))
        json.loads(cleaned)


class FirewallScriptTest(unittest.TestCase):
    def test_local_teams_snat_to_gateway(self):
        text = ra.remote_access_firewall_script(["101", "102"], [], "10.0.0.252")
        self.assertIn("-s 100.64.0.0/10 -d 192.168.0.0/16 -j ACCEPT", text)
        self.assertIn("-d 192.168.101.0/24 -j SNAT --to-source 192.168.101.1", text)
        self.assertIn("-d 192.168.102.0/24 -j SNAT --to-source 192.168.102.1", text)
        self.assertNotIn("--to-source 10.0.0.252", text)

    def test_satellite_teams_snat_to_engine_mgmt(self):
        text = ra.remote_access_firewall_script(["101"], ["130"], "10.0.0.252")
        self.assertIn("-d 192.168.130.0/24 -j SNAT --to-source 10.0.0.252", text)
        self.assertIn("-d 192.168.101.0/24 -j SNAT --to-source 192.168.101.1", text)
        self.assertEqual(text.count("-C FORWARD"), 1)  # one blanket tailnet accept

    def test_idempotent_pairs(self):
        text = ra.remote_access_firewall_script(["101"], [], "10.0.0.252")
        for line in text.splitlines():
            if " -C " in line or " -t nat -C " in line:
                self.assertIn("2>/dev/null || ", line)


class LoadRosterTest(unittest.TestCase):
    def test_absent_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(ra.load_roster(Path(td)), {})

    def test_valid_roundtrip_and_normalization(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "people.json").write_text(
                json.dumps({"team1": ["Alice Smith", "BOB"], "team2": ["Carol"]}))
            roster = ra.load_roster(Path(td))
        self.assertEqual(roster, {"team1": ["alice-smith", "bob"], "team2": ["carol"]})

    def test_bad_name_refused(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "people.json").write_text('{"team1": ["bad name!"]}')
            with self.assertRaises(SystemExit):
                ra.load_roster(Path(td))

    def test_duplicate_refused(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "people.json").write_text('{"team1": ["alice"], "team2": ["alice"]}')
            with self.assertRaises(SystemExit):
                ra.load_roster(Path(td))

    def test_unknown_team_refused(self):
        ctx = SimpleNamespace(comp_dir=Path(tempfile.mkdtemp()), teams={"team1": {"identifier": 101}})
        (ctx.comp_dir / "people.json").write_text('{"team9": ["alice"]}')
        with self.assertRaises(SystemExit):
            ra._comp_teams(ctx)

    def test_comp_teams_shape(self):
        ctx = SimpleNamespace(comp_dir=Path(tempfile.mkdtemp()),
                              teams={"team1": {"identifier": 101},
                                     "team2": {"identifier": 102}})
        (ctx.comp_dir / "people.json").write_text('{"team2": ["carol"]}')
        teams = ra._comp_teams(ctx)
        self.assertEqual(teams, [{"identifier": "101", "participants": []},
                                 {"identifier": "102", "participants": ["carol"]}])


@contextmanager
def _hs_patch(routes=None, nodes=None, users=None, policy_text=""):
    """Patch both IO surfaces (hs_json and hs_cli) — offline, no sshpass anywhere."""
    def fake_hs_json(args, timeout=90, check=True):
        if args.startswith("nodes list-routes"):
            return routes or []
        if args.startswith("nodes list"):
            return nodes or []
        if args.startswith("users list"):
            return users or []
        raise AssertionError(f"unexpected hs_json call: {args}")

    def fake_hs_cli(args, timeout=90, check=True):
        return SimpleNamespace(returncode=0, stdout=policy_text, stderr="")

    with patch.object(ra, "hs_json", side_effect=fake_hs_json), \
            patch.object(ra, "hs_cli", side_effect=fake_hs_cli):
        yield


class RouteConflictTest(unittest.TestCase):
    def test_foreign_router_conflicts(self):
        routes = [{"name": "eng-other-comp",
                   "approved_routes": ["192.168.101.0/24"], "subnet_routes": []}]
        with _hs_patch(routes=routes):
            found = ra.route_conflicts("mine", ["101", "102"])
        self.assertEqual(found, ["192.168.101.0/24 advertised by 'eng-other-comp'"])

    def test_own_router_excluded(self):
        routes = [{"name": "eng-mine",
                   "approved_routes": ["192.168.101.0/24"], "subnet_routes": []}]
        with _hs_patch(routes=routes):
            self.assertEqual(ra.route_conflicts("mine", ["101"]), [])

    def test_clean_when_no_overlap(self):
        routes = [{"name": "eng-other", "approved_routes": ["192.168.199.0/24"],
                   "subnet_routes": []}]
        with _hs_patch(routes=routes):
            self.assertEqual(ra.route_conflicts("mine", ["101"]), [])


class VerifyRemoteAccessTest(unittest.TestCase):
    STATE = {"remote_access": {"enabled": True, "comp_id": "mine",
                               "router_node": "eng-mine"}}

    def test_disabled_is_skip(self):
        ok, problems = ra.verify_remote_access({}, "mine", ["101"])
        self.assertIsNone(ok)

    def test_all_serving_passes(self):
        routes = [{"name": "eng-mine",
                   "approved_routes": ["192.168.101.0/24"],
                   "subnet_routes": ["192.168.101.0/24"]}]
        with _hs_patch(routes=routes):
            ok, problems = ra.verify_remote_access(self.STATE, "mine", ["101"])
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_unapproved_route_fails(self):
        routes = [{"name": "eng-mine", "approved_routes": [], "subnet_routes": []}]
        with _hs_patch(routes=routes):
            ok, problems = ra.verify_remote_access(self.STATE, "mine", ["101"])
        self.assertFalse(ok)
        self.assertTrue(any("not SERVING" in p for p in problems))

    def test_missing_router_fails(self):
        with _hs_patch(routes=[]):
            ok, problems = ra.verify_remote_access(self.STATE, "mine", ["101"])
        self.assertFalse(ok)
        self.assertTrue(any("not registered" in p for p in problems))


class TeardownTest(unittest.TestCase):
    def test_absent_record_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            ra.teardown_remote_access(Path(td))  # no state file at all: returns silently

    def test_disabled_record_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".deploy_state.json").write_text(
                json.dumps({"remote_access": {"enabled": False}}))
            ra.teardown_remote_access(Path(td))


class EnrollmentMarkdownTest(unittest.TestCase):
    def test_contains_per_person_commands(self):
        md = ra.enrollment_markdown("cde", "https://hs.example", [
            {"person": "alice", "team": "101", "key": "hskey-auth-xyz"}])
        self.assertIn("## alice (team 101)", md)
        self.assertIn("tailscale up --login-server https://hs.example "
                      "--authkey hskey-auth-xyz", md)


if __name__ == "__main__":
    unittest.main()
