"""the AD lineup wiring: Windows service mappings, post-domain AD configs, and
cross-box ("ip:<box>") identity vars for airship-webapp's DB_HOST (box names follow the
catalog config's own required-var names).

Offline: build_event_conf needs no I/O, and generate_stage_configs runs against a
written nakon-config.json (Proxmox/Nakon boundaries mocked upstream)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from nakonops import nakon_config_ops
from quotient.setup import build_event_conf, expected_service_names

BOXES = [
    {"name": "mira", "last_octet": 2, "template": "base-windows-server-2019"},
    {"name": "skeld", "last_octet": 3, "template": "base-windows-server"},
    {"name": "airship", "last_octet": 4, "template": "base-centos8-fix"},
    {"name": "polus", "last_octet": 5, "template": "base-ubuntu20.04-fix"},
]
TEAMS = {"team1": {"identifier": "1400", "password": "x"},
         "team2": {"identifier": "1401", "password": "x"}}

CTX = {
    "teams": TEAMS,
    "boxes_per_team": BOXES,
    "team_passwords": {"team1": "x", "team2": "x"},
    "event_name": "t",
    "quotient_admin_password": "x",
}


def _by_box(conf, box):
    return next(b for b in conf["box"] if b["name"] == box)


class CheckMappings(unittest.TestCase):

    def test_ad_dns_localhost_scores_dns_record(self):
        conf = build_event_conf(CTX, {"mira": ["ad-dns-localhost"]})
        checks = _by_box(conf, "mira")["Dns"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["Port"], 53)
        self.assertEqual(checks[0]["Record"],
                         [{"Kind": "A", "Domain": "localhost",
                           "Answer": ["127.0.0.1"]}])



def _write_machines(comp_dir, configs_by_box):
    machines = []
    for team, meta in TEAMS.items():
        n = meta["identifier"]
        for box, octet in (("mira", 2), ("skeld", 3), ("airship", 4), ("polus", 5)):
            machines.append({"name": f"{box}-{team}", "ip": f"192.168.{n}.{octet}",
                             "configurations": list(configs_by_box[box])})
    (Path(comp_dir) / "nakon-config.json").write_text(
        json.dumps({"machines": machines}))


def _stage_names(path):
    return {m["name"]: [c if isinstance(c, str) else c["name"]
                        for c in m["configurations"]]
            for m in json.loads(Path(path).read_text())["machines"]}


class CrossBoxIdentityVars(unittest.TestCase):
    def test_db_host_filled_per_team_and_excluded_from_golden(self):
        pins = {
            "mira": [], "skeld": [],
            "airship": [{"name": "airship-webapp",
                         "vars": {"DB_USER": "airship", "DB_PASS": "airship"}}],
            "polus": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            _write_machines(tmp, pins)
            golden_p, predomain_p, final_p, _, _ = nakon_config_ops.generate_stage_configs(
                Path(tmp), TEAMS, BOXES)
            golden_configs = [c for cfgs in _stage_names(golden_p).values()
                              for c in cfgs]
            self.assertNotIn("airship-webapp", golden_configs,
                             "airship-webapp is identity-dependent and must not ride "
                             "the golden disk")
            predomain = {m["name"]: m["configurations"]
                      for m in json.loads(Path(predomain_p).read_text())["machines"]}
            self.assertIn("airship-team1", predomain)
            self.assertEqual(
                predomain["airship-team1"][0]["vars"]["DB_HOST"], "192.168.1400.5")
            self.assertEqual(
                predomain["airship-team2"][0]["vars"]["DB_HOST"], "192.168.1401.5")

    def test_missing_cross_box_target_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            machines = [
                {"name": f"airship-{team}", "ip": f"192.168.{meta['identifier']}.4",
                 "configurations": [{"name": "airship-webapp",
                                     "vars": {"DB_USER": "a", "DB_PASS": "b"}}]}
                for team, meta in TEAMS.items()]
            (Path(tmp) / "nakon-config.json").write_text(
                json.dumps({"machines": machines}))
            with self.assertRaises(SystemExit):
                nakon_config_ops.generate_stage_configs(Path(tmp), TEAMS, BOXES)


class PostDomainStageRouting(unittest.TestCase):
    def test_ad_dependent_configs_plant_in_final_stage(self):
        pins = {
            "mira": ["SMB v1", "ad-color-fleet-win", "guest-enabled-win",
                     "Elevate Guest Account", "weak-password-policy-win",
                     "ad-dns-localhost"],
            "skeld": [], "airship": [], "polus": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            _write_machines(tmp, pins)
            golden_p, predomain_p, final_p, _, _ = nakon_config_ops.generate_stage_configs(
                Path(tmp), TEAMS, BOXES, unbooted={"mira"})
            self.assertNotIn("mira-golden", _stage_names(golden_p),
                             "the DC's golden stays generalized: no golden machine")
            final = _stage_names(final_p)
            mira_configs = final["mira-team1"]
            for name in ("ad-color-fleet-win", "guest-enabled-win",
                         "Elevate Guest Account", "weak-password-policy-win",
                         "ad-dns-localhost"):
                self.assertIn(name, mira_configs)
            # SMB v1 is not AD-dependent: as a cold box's otherwise golden-stage
            # config it plants per team pre-domain (pre-domain stage) instead.
            predomain = _stage_names(predomain_p)
            self.assertEqual(predomain["mira-team1"], ["SMB v1"])


if __name__ == "__main__":
    unittest.main()
