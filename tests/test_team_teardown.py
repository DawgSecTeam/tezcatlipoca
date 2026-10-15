"""teardown/team_ops.py: the mid-event single-team removal.

Offline: terraform, the Proxmox API, SSH and Quotient are all faked. What is pinned —
the blast-radius rails (other teams' instances are never named in the -target list, a
missing roster entry or a team the state doesn't own is a Refusal, a surviving clone
fails the run BEFORE Quotient is touched) and the state bookkeeping (teams.json,
.deploy_state.json teams/deleted_teams, terraform.tfvars.json all lose the team).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from infra.ownership import Refusal  # noqa: E402
from teardown import team_ops  # noqa: E402


def _tf_state():
    return {"resources": [
        {"type": "proxmox_virtual_environment_vm", "name": "team_box", "instances": [
            {"index_key": "team1-web01"}, {"index_key": "team1-db01"},
            {"index_key": "101-web01"}, {"index_key": "101-db01"},
            {"index_key": "102-web01"}]},
        {"type": "proxmox_network_linux_bridge", "name": "team_bridge", "instances": [
            {"index_key": "team1"}, {"index_key": "team2"}]},
        {"type": "proxmox_network_linux_bridge", "name": "transit_bridge", "instances": [
            {"index_key": "team2"}]},
    ]}


class TeamTargetsFromState(unittest.TestCase):
    def test_only_the_teams_instances_are_targeted(self):
        targets, bridges = team_ops.team_targets_from_state(
            _tf_state(), "team1", {"identifier": 101})
        self.assertEqual(sorted(targets),
                         ['proxmox_virtual_environment_vm.team_box["team1-db01"]',
                          'proxmox_virtual_environment_vm.team_box["team1-web01"]'])
        self.assertEqual(bridges, ['proxmox_network_linux_bridge.team_bridge["team1"]'])

    def test_identifier_teams_target_by_identifier_never_by_prefix_collision(self):
        # identifier 101's prefix must not match team 102's clones.
        targets, _ = team_ops.team_targets_from_state(
            _tf_state(), "team101x", {"identifier": 101})
        self.assertEqual(sorted(targets),
                         ['proxmox_virtual_environment_vm.team_box["101-db01"]',
                          'proxmox_virtual_environment_vm.team_box["101-web01"]'])

    def test_satellite_slot_targets_its_own_resource(self):
        state = {"resources": [
            {"type": "proxmox_virtual_environment_vm", "name": "team_box_sat1", "instances": [
                {"index_key": "102-web01"}]},
            {"type": "proxmox_network_linux_bridge", "name": "team_bridge_sat1", "instances": [
                {"index_key": "team102x"}]},
        ]}
        targets, bridges = team_ops.team_targets_from_state(
            state, "team102x", {"identifier": 102, "slot": 1})
        self.assertEqual(targets, ['proxmox_virtual_environment_vm.team_box_sat1["102-web01"]'])
        self.assertEqual(bridges,
                         ['proxmox_network_linux_bridge.team_bridge_sat1["team102x"]'])

    def test_a_team_with_no_state_instances_targets_nothing(self):
        targets, bridges = team_ops.team_targets_from_state(
            _tf_state(), "team3", {"identifier": 103})
        self.assertEqual((targets, bridges), ([], []))


def _comp_dir(d, team="team2"):
    comp = Path(d)
    (comp / "Compfile").write_text("name probe\nscenario probe\ndifficulty 3\n")
    (comp / "teams.json").write_text(json.dumps({
        "team1": {"identifier": 101, "password": "pw1"},
        "team2": {"identifier": 102, "password": "pw2", "slot": 0}}))
    (comp / "boxes.json").write_text(json.dumps([{"name": "web01", "template": "t"}]))
    (comp / "box_services.json").write_text("{}")
    (comp / ".deploy_state.json").write_text(json.dumps({
        "run_id": "run-abc123", "last_phase": 8,
        "teams": {"team1": {"identifier": 101, "password": "pw1"},
                  "team2": {"identifier": 102, "password": "pw2"}},
        "admin_password": "a", "scoring_password": "s", "postgres_password": "p",
        "redis_password": "r", "box_creds": {"admin": "c"}}))
    tdir = comp / "terraform"
    tdir.mkdir()
    (tdir / "terraform.tfstate").write_text(json.dumps(_tf_state()))
    (tdir / "terraform.tfvars.json").write_text(json.dumps({
        "teams": {"team1": {"identifier": 101}, "team2": {"identifier": 102}}}))
    return comp


def _run(comp, team_key, *, run_terraform_rc=0, alive_vms=(), env=None):
    env = env or {"TF_VAR_proxmox_node": "pve"}
    proc = mock.Mock(returncode=run_terraform_rc)
    with mock.patch.object(team_ops, "load_ownership",
                           return_value=(json.loads(
                               (comp / ".deploy_state.json").read_text()),
                               mock.Mock(run_id="run-abc123", tags={"tezcatlipoca", "probe", "run-abc123"}))), \
            mock.patch.object(team_ops, "pre_stop_windows_boxes") as p_stop, \
            mock.patch.object(team_ops, "terraform_init"), \
            mock.patch.object(team_ops, "clear_stale_state_lock"), \
            mock.patch.object(team_ops, "run_terraform", return_value=proc) as p_tf, \
            mock.patch("infra.client.proxmox_api",
                       return_value={"data": [
                           {"name": v, "vmid": 999} for v in alive_vms]}) as p_api, \
            mock.patch.object(team_ops, "read_terraform_ctx",
                              return_value={"scoring_engine_ip": "10.0.0.9",
                                            "ssh_key_path": "/k", "vm_username": "ubuntu"}), \
            mock.patch.object(team_ops, "_deactivate_team") as p_deact, \
            mock.patch.object(team_ops, "_refresh_engine_conf") as p_conf, \
            mock.patch.object(team_ops, "_refresh_portal_config") as p_portal:
        lines = []
        report = team_ops.run_team_teardown(comp, team_key, env=env,
                                            log=lines.append)
    return report, {
        "stop": p_stop, "tf": p_tf, "api": p_api, "deact": p_deact,
        "conf": p_conf, "portal": p_portal, "lines": lines,
    }


class RunTeamTeardown(unittest.TestCase):
    def test_unknown_team_is_refused_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            with self.assertRaises(Refusal):
                _run(comp, "team9")
            # nothing was written
            self.assertIn("team2", json.loads((comp / "teams.json").read_text()))

    def test_a_team_the_state_does_not_own_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            state = json.loads((comp / ".deploy_state.json").read_text())
            state["teams"] = {"team1": state["teams"]["team1"]}
            (comp / ".deploy_state.json").write_text(json.dumps(state))
            with self.assertRaises(Refusal):
                _run(comp, "team2")

    def test_the_destroy_targets_only_this_teams_instances(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, calls = _run(comp, "team2")
        self.assertTrue(report["clean"])
        argv = calls["tf"].call_args.args[0]
        targets = [a for a in argv if a.startswith("-target=")]
        self.assertIn('-target=proxmox_virtual_environment_vm.team_box["102-web01"]', targets)
        self.assertFalse(any("101-" in t or "team1-" in t for t in targets),
                         f"another team's instance targeted: {targets}")
        self.assertIn('-target=proxmox_network_linux_bridge.team_bridge["team2"]', targets)
        self.assertIn('-target=proxmox_network_linux_bridge.transit_bridge["team2"]', targets)
        calls["deact"].assert_called_once()
        calls["conf"].assert_called_once()
        calls["portal"].assert_called_once()

    def test_prestop_narrowed_to_the_one_team(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, calls = _run(comp, "team2")
        teams_arg = calls["stop"].call_args.args[0]
        self.assertEqual(list(teams_arg), ["team2"])

    def test_a_surviving_clone_fails_before_quotient_is_touched(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, calls = _run(comp, "team2", alive_vms=["102-web01"])
            self.assertFalse(report["clean"])
            self.assertTrue(any("still on pve" in p for p in report["problems"]))
            calls["deact"].assert_not_called()
            calls["conf"].assert_not_called()
            # the roster still carries the team
            self.assertIn("team2", json.loads((comp / "teams.json").read_text()))

    def test_failed_destroy_reports_unclean(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, calls = _run(comp, "team2", run_terraform_rc=1)
        self.assertFalse(report["clean"])
        self.assertTrue(any("terraform destroy" in p for p in report["problems"]))
        calls["deact"].assert_not_called()

    def test_the_roster_records_the_deletion_everywhere(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, _ = _run(comp, "team2")
            teams = json.loads((comp / "teams.json").read_text())
            state = json.loads((comp / ".deploy_state.json").read_text())
            tfvars = json.loads((comp / "terraform" / "terraform.tfvars.json").read_text())
        self.assertEqual(list(teams), ["team1"])
        self.assertNotIn("team2", state["teams"])
        self.assertEqual(state["deleted_teams"][0]["team"], "team2")
        self.assertEqual(list(tfvars["teams"]), ["team1"])
        self.assertTrue(report["clean"])


if __name__ == "__main__":
    unittest.main()
