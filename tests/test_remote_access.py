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

from net import remote_access_ops as ra  # noqa: E402
from estate import state as estate_state  # noqa: E402


COMPS_ONE = [{"comp_id": "cde-2026", "engine_ip": "10.0.0.248",
              "teams": [{"identifier": "101", "participants": ["alice", "bob"]},
                        {"identifier": "102", "participants": []}]}]

COMPS_TWO = [{"comp_id": "cde-2026", "engine_ip": "10.0.0.248",
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
        self.assertEqual(text.count('"src"'), 1)
        self.assertIn('{"src": ["group:full-access"], "dst": ["*"], "ip": ["*"]},', text)

    def test_participant_team_gets_scoped_grants(self):
        text = ra.render_policy(FULL_ACCESS, COMPS_ONE)
        # participants keyed by USERNAME (@-form), not node tag
        self.assertIn('"comp-cde-2026-alice@"', text)
        self.assertIn('"comp-cde-2026-bob@"', text)
        self.assertIn('{"src": ["comp-cde-2026-alice@", "comp-cde-2026-bob@"], '
                      '"dst": ["192.168.101.0/24"], "ip": ["*"]},', text)
        self.assertIn('"dst": ["10.0.0.248"], "ip": ["80"]},', text)
        # team 102 has no participants: no grant lines
        self.assertNotIn("192.168.102.0/24", text)
        self.assertEqual(text.count('"src"'), 3)

    def test_two_comps_deterministic_and_scoped(self):
        a = ra.render_policy(FULL_ACCESS, COMPS_TWO)
        b = ra.render_policy(FULL_ACCESS, list(reversed(COMPS_TWO)))
        self.assertEqual(a, b)  # sorted by comp id: output order is canonical
        self.assertIn('"comp-scrim-two-carol@"', a)
        self.assertIn('"dst": ["192.168.130.0/24"], "ip": ["*"]', a)
        self.assertIn('"dst": ["10.0.0.250"], "ip": ["80"]', a)

    def test_engine_ip_without_prefix(self):
        comps = [{"comp_id": "x", "engine_ip": "10.0.0.7",
                  "teams": [{"identifier": "101", "participants": ["a"]}]}]
        self.assertIn('"dst": ["10.0.0.7"], "ip": ["80"]', ra.render_policy(FULL_ACCESS, comps))

    def test_valid_hujson_up_to_comments(self):
        # strip // comments and trailing commas -> must parse as strict JSON
        text = ra.render_policy(FULL_ACCESS, COMPS_TWO)
        lines = [ln.split("//")[0].rstrip() for ln in text.splitlines()]
        cleaned = re.sub(r",\s*([}\]])", r"\1", "\n".join(lines))
        json.loads(cleaned)


class FirewallScriptTest(unittest.TestCase):
    def test_local_teams_snat_to_gateway(self):
        text = ra.remote_access_firewall_script(["101", "102"], [], "10.0.0.248")
        self.assertIn("-s 100.64.0.0/10 -d 192.168.0.0/16 -j ACCEPT", text)
        self.assertIn("-d 192.168.101.0/24 -j SNAT --to-source 192.168.101.1", text)
        self.assertIn("-d 192.168.102.0/24 -j SNAT --to-source 192.168.102.1", text)
        self.assertNotIn("--to-source 10.0.0.248", text)

    def test_satellite_teams_snat_to_engine_mgmt(self):
        text = ra.remote_access_firewall_script(["101"], ["130"], "10.0.0.248")
        self.assertIn("-d 192.168.130.0/24 -j SNAT --to-source 10.0.0.248", text)
        self.assertIn("-d 192.168.101.0/24 -j SNAT --to-source 192.168.101.1", text)
        self.assertEqual(text.count("-C FORWARD"), 1)  # one blanket tailnet accept

    def test_idempotent_pairs(self):
        text = ra.remote_access_firewall_script(["101"], [], "10.0.0.248")
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
    # patches both IO surfaces (hs_json and hs_cli) — offline, no sshpass anywhere
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
                               "router_node": "eng-mine",
                               "participants": ["comp-mine-alice"]}}

    def test_disabled_is_skip(self):
        ok, problems = ra.verify_remote_access({}, "mine", ["101"])
        self.assertIsNone(ok)

    def test_all_serving_passes(self):
        routes = [{"name": "eng-mine",
                   "approved_routes": ["192.168.101.0/24"],
                   "subnet_routes": ["192.168.101.0/24"]}]
        with _hs_patch(routes=routes, policy_text='"comp-mine-alice@"'):
            ok, problems = ra.verify_remote_access(self.STATE, "mine", ["101"])
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_policy_missing_participant_fails(self):
        routes = [{"name": "eng-mine",
                   "approved_routes": ["192.168.101.0/24"],
                   "subnet_routes": ["192.168.101.0/24"]}]
        with _hs_patch(routes=routes, policy_text=""):
            ok, problems = ra.verify_remote_access(self.STATE, "mine", ["101"])
        self.assertFalse(ok)
        self.assertTrue(any("grant" in p for p in problems))

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
            ra.teardown_remote_access(Path(td))  # no state document at all: returns silently

    def test_disabled_record_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            estate_state.write_flat(Path(td), {"remote_access": {"enabled": False}})
            ra.teardown_remote_access(Path(td))


class EnrollmentMarkdownTest(unittest.TestCase):
    def test_contains_per_person_commands(self):
        md = ra.enrollment_markdown("cde", "https://hs.example", [
            {"person": "alice", "team": "101", "key": "hskey-auth-xyz"}])
        self.assertIn("## alice (team 101)", md)
        self.assertIn("tailscale up --login-server https://hs.example "
                      "--authkey hskey-auth-xyz", md)


class RebindingReassertTest(unittest.TestCase):
# tailscaled re-applies its ruleset when an interface gains an address
#     ("LinkChange: major, rebinding: ... rebind-reason=[ips-changed]"), taking the
#     POSTROUTING chain with it; the 30s timer left tailnet->team traffic broken inside
#     every window (caught live: present 9 samples, absent 3, present).

    def test_the_monitor_reasserts_on_the_interface_event(self):
        script = ra.remote_access_monitor_script()
        self.assertIn("ip monitor address link", script)
        self.assertIn("/usr/local/sbin/range-remote-access.sh", script)
        # restarted if ip monitor's socket dies
        self.assertIn("while :; do", script)

    def test_the_monitor_unit_runs_that_script_and_restarts(self):
        unit = ra.remote_access_monitor_unit()
        self.assertIn("ExecStart=/usr/local/sbin/range-remote-access-monitor.sh", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_the_timer_stays_as_the_fallback(self):
        _service, timer = ra.remote_access_systemd_units()
        self.assertIn("OnUnitActiveSec=30", timer)

    def test_the_firewall_script_names_both_triggers(self):
        text = ra.remote_access_firewall_script(["101"], [], "10.0.0.248")
        self.assertIn("range-remote-access-monitor.service", text)
        self.assertIn("range-remote-access.timer", text)

    def test_push_installs_and_enables_the_monitor_alongside_the_timer(self):
        calls = []
        with patch.object(ra, "_engine_cmd",
                          side_effect=lambda ctx, cmd, **kw: calls.append(cmd)):
            ra.push_remote_access_firewall({}, "10.0.0.248", ["101"], ["130"])
        install = next(c for c in calls if "range-remote-access-monitor.sh" in c)
        self.assertIn("/etc/systemd/system/range-remote-access-monitor.service", install)
        self.assertIn("enable --now range-remote-access.timer "
                      "range-remote-access-monitor.service", install)
        self.assertIn("/etc/systemd/system/range-remote-access.timer", install)


class TailscaleInstallTest(unittest.TestCase):
    def _install_command(self):
        calls = []
        with patch.object(ra, "_engine_cmd",
                          side_effect=lambda ctx, cmd, **kw: calls.append(cmd)), \
             patch.object(ra, "engine_tailscale_enrolled", return_value=False), \
             patch.object(ra, "_infra_user_id", return_value=7), \
             patch.object(ra, "_mint_preauth_key", return_value="hskey"), \
             patch.object(ra.config, "require", return_value="https://hs.example"):
            ra.enroll_engine_router(SimpleNamespace(), "comp", ["151"])
        return next(c for c in calls if "install.sh" in c)

    def test_the_install_waits_out_the_clones_apt_locks_first(self):
        """The engine clone's first boot holds the dpkg locks for minutes, so the
        installer's apt-get died on the lock after the old three 5-second retries and the
        step ended 127 (caught live: the run that found it). The lock wait comes first."""
        cmd = self._install_command()
        self.assertIn("cloud-init status --wait", cmd)
        self.assertIn("flock -n /var/lib/dpkg/lock-frontend", cmd)
        self.assertIn("flock -n /var/lib/dpkg/lock", cmd)
        self.assertLess(cmd.index("flock -n"), cmd.index("install.sh"))
        # A genuinely wedged apt still fails loudly with the installer's own log.
        self.assertIn("exit 127", cmd)
        self.assertIn("cat /tmp/ts-install.log", cmd)


if __name__ == "__main__":
    unittest.main()
