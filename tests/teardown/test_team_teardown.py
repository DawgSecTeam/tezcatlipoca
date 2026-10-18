"""teardown/team_ops.py: the mid-event single-team removal.

Offline: the Proxmox API, SSH and Quotient are all faked. What is pinned — the blast-radius
rails (only this team's recorded instances are destroyed, a missing roster entry or a team the
state doesn't own is a Refusal, a surviving clone fails the run BEFORE Quotient is touched) and
the state bookkeeping (teams.json and the state document's roster lose the team).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from estate import state as estate_state  # noqa: E402
from infra.ownership import Ownership, Refusal, bridge_comment  # noqa: E402
from teardown import team_ops  # noqa: E402

COMP = "probe"
RUN = "run-abc123"
OURS = Ownership(COMP, RUN)
TAGS = sorted(OURS.tags)


class FakePVE:
# The node's VMs and bridges, mutable so a destroy is observable; a vmid in
#     `survivors` ignores its DELETE (a clone that would not die), and `config_tags`
#     overrides every VM's config tags (a foreign-tagged clone).
    def __init__(self, vms, bridges=(), survivors=(), config_tags=None):
        self.vms = {int(v["vmid"]): dict(v) for v in vms}
        self.bridges = set(bridges)
        self.survivors = set(survivors)
        self.config_tags = config_tags
        self.deleted = []
        self.deleted_bridges = []

    def __call__(self, method, path, **kw):
        if method == "GET" and path.endswith("/qemu"):
            return {"data": [dict(v) for v in self.vms.values()]}
        if method == "GET" and path.endswith("/config"):
            vmid = int(path.split("/")[-2])
            tags = self.config_tags if self.config_tags is not None \
                else ";".join(self.vms[vmid]["tags"])
            return {"data": {"tags": tags}}
        if method == "GET" and path.endswith("/network"):
            return {"data": [{"iface": b, "comments": bridge_comment(COMP, RUN, "team")}
                             for b in self.bridges]}
        if method == "DELETE" and "/network/" in path:
            bridge = path.rsplit("/", 1)[1]
            self.bridges.discard(bridge)
            self.deleted_bridges.append(bridge)
            return {"data": None}
        if method == "DELETE" and "/qemu/" in path:
            vmid = int(path.split("/qemu/")[1].split("/")[0])
            self.deleted.append(vmid)
            if vmid not in self.survivors:
                self.vms.pop(vmid, None)
            return {"data": None}
        return {"data": None}


def _comp_dir(d):
    comp = Path(d)
    (comp / "Compfile").write_text("name probe\nscenario probe\ndifficulty 3\n")
    (comp / "teams.json").write_text(json.dumps({
        "team1": {"identifier": 101, "password": "pw1"},
        "team2": {"identifier": 102, "password": "pw2", "slot": 0}}))
    (comp / "boxes.json").write_text(json.dumps([{"name": "web01", "template": "t"}]))
    (comp / "box_services.json").write_text("{}")
    doc = estate_state.EstateState.load(comp)
    doc.absorb({
        "comp_name": COMP, "run_id": RUN, "last_phase": 8,
        "scoring_vm_id": 1550, "engine_mgmt_ip": "10.0.0.9",
        "teams": {"team1": {"identifier": 101, "password": "pw1"},
                  "team2": {"identifier": 102, "password": "pw2"}},
        "admin_password": "a", "scoring_password": "s", "postgres_password": "p",
        "redis_password": "r", "box_creds": {"admin": "c"}})
    doc.record("team_box", "pve", "101-web01", vmid=1011, tags=TAGS)
    doc.record("team_box", "pve", "101-db01", vmid=1012, tags=TAGS)
    doc.record("team_box", "pve", "102-web01", vmid=1021, tags=TAGS)
    doc.record("team_box", "pve", "102-db01", vmid=1022, tags=TAGS)
    doc.record("bridge", "pve", "vmbr101", bridge="vmbr101")
    doc.record("bridge", "pve", "vmbr102", bridge="vmbr102")
    doc.record("bridge", "pve", "vmbrW102", bridge="vmbrW102")
    doc.save()
    return comp


def _run(comp, team_key, *, survivors=(), config_tags=None, env=None):
    env = env or {"TEZ_PROXMOX_NODE": "pve"}
    doc = estate_state.EstateState.load(comp)
    fake = FakePVE(
        [{"vmid": r["vmid"], "name": r["name"], "status": "running", "tags": r["tags"]}
         for r in doc.resources("team_box")],
        bridges=[r["bridge"] for r in doc.resources("bridge")],
        survivors=survivors, config_tags=config_tags)
    # The default node comes from the config, not the env dict handed to the teardown.
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(team_ops, "load_ownership",
                              return_value=(estate_state.load_flat(comp), OURS)), \
            mock.patch.object(team_ops, "pre_stop_windows_boxes") as p_stop, \
            mock.patch.object(team_ops, "_engine_ctx",
                              return_value={"scoring_engine_ip": "10.0.0.9"}), \
            mock.patch("infra.client.proxmox_api", fake), \
            mock.patch("infra.ownership.proxmox_api", fake), \
            mock.patch("infra.ownership.wait_for_proxmox_task", lambda *a, **k: None), \
            mock.patch.object(team_ops, "_deactivate_team") as p_deact, \
            mock.patch.object(team_ops, "_refresh_engine_conf") as p_conf, \
            mock.patch.object(team_ops, "_refresh_portal_config") as p_portal:
        lines = []
        report = team_ops.run_team_teardown(comp, team_key, env=env, log=lines.append)
    return report, {
        "stop": p_stop, "fake": fake, "deact": p_deact,
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
            doc = estate_state.EstateState.load(comp)
            doc.section("secrets")["teams"] = {"team1": {"identifier": 101}}
            doc.save()
            with self.assertRaises(Refusal):
                _run(comp, "team2")

    def test_the_destroy_targets_only_this_teams_instances(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, calls = _run(comp, "team2")
        self.assertTrue(report["clean"])
        self.assertEqual(calls["fake"].deleted, [1021, 1022])
        self.assertNotIn(1011, calls["fake"].deleted)
        self.assertEqual(sorted(calls["fake"].deleted_bridges), ["vmbr102", "vmbrW102"])
        calls["deact"].assert_called_once()
        calls["conf"].assert_called_once()
        calls["portal"].assert_called_once()


    def test_a_surviving_clone_fails_before_quotient_is_touched(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, calls = _run(comp, "team2", survivors={1021})
            self.assertFalse(report["clean"])
            self.assertTrue(any("still on pve" in p for p in report["problems"]))
            calls["deact"].assert_not_called()
            calls["conf"].assert_not_called()
            # the roster still carries the team
            self.assertIn("team2", json.loads((comp / "teams.json").read_text()))

    def test_failed_destroy_reports_unclean(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, calls = _run(comp, "team2",
                                 config_tags="tezcatlipoca;comp-probe;run-other")
        self.assertFalse(report["clean"])
        self.assertTrue(any("102-web01" in p for p in report["problems"]))
        calls["deact"].assert_not_called()

    def test_the_roster_records_the_deletion_everywhere(self):
        with tempfile.TemporaryDirectory() as d:
            comp = _comp_dir(d)
            report, _ = _run(comp, "team2")
            teams = json.loads((comp / "teams.json").read_text())
            doc = json.loads((comp / "estate-state.json").read_text())
        self.assertEqual(list(teams), ["team1"])
        self.assertNotIn("team2", doc["secrets"]["teams"])
        self.assertEqual(doc["run"]["deleted_teams"][0]["team"], "team2")
        self.assertTrue(report["clean"])


if __name__ == "__main__":
    unittest.main()
