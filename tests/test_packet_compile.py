"""Packet-profile compilation + the pin semantics it leans on (score-only, credlist
twins, packet credentials, domain knob, baseline accounts). Offline — no vulndb,
no Proxmox, no engine."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import packet_ops
from packet_ops import (build_baseline, compile_profile, load_profile, team_domain_parts,
                        validate_profile)
from quotient.setup import build_event_conf, expected_service_names
import nakon_ops
from domain_ops import team_domain

PROFILE = """
packet:
  source: test packet
event:
  comp_id: pkt-test-comp
  name: Packet Test
  scenario: test scenario line
  difficulty: 4
domain:
  name_template: "mira-{team}.corp.sus"
credentials:
  box_username: blueteam
  box_password: "n0t_sus1"
  credlists:
    linux:
      blueteam: "n0t_sus1"
      airship: airship
    domain:
      blueteam: "n0t_sus!"
  domain_accounts:
    - {username: blueteam, password: "n0t_sus!", admin: true, full_name: Lead}
  out_of_scope: [scorebot, blackteam, red_scoring]
boxes:
  - {name: fw01, packet_os: pfSense, template: pfsense, unmanaged: true, last_octet: 1, fidelity: substituted}
  - {name: ad01, packet_os: "WS2019", template: base-windows-server, last_octet: 2, cpu: 2, memory_mb: 4096, disk_gb: 60, disk_iface: sata0, domain_role: dc, fidelity: substituted}
  - {name: web01, packet_os: "CentOS 8", template: base-fedora44-fix, last_octet: 4, fidelity: substituted}
  - {name: db01, packet_os: "Ubuntu 20.04", template: base-ubuntu24.04-fix, last_octet: 5, fidelity: substituted}
services:
  - {box: ad01, name: LDAP, port: 389, pin: ADDS, display: ldap, fidelity: exact}
  - {box: ad01, name: DNS, port: 53, pin: score/tcp, display: dns, fidelity: substituted}
  - {box: web01, name: Web, port: 80, pin: apache, display: http, fidelity: exact}
  - {box: web01, name: SSH, port: 22, pin: ssh, display: ssh, dual_credit: true, fidelity: substituted}
  - {box: db01, name: MySQL, port: 3306, pin: mysql, display: sql, fidelity: exact}
injects:
  - slug: 01-welcome
    title: Welcome
    open_offset_min: 0
    due_offset_min: 30
    close_offset_min: 60
    briefing: "hello"
"""

BOXES = [
    {"name": "fw01", "last_octet": 1, "cpu": 1, "memory_mb": 2048, "disk_gb": None,
     "template": "pfsense", "unmanaged": True},
    {"name": "ad01", "last_octet": 2, "cpu": 2, "memory_mb": 4096, "disk_gb": 60,
     "disk_iface": "sata0", "template": "base-windows-server"},
    {"name": "web01", "last_octet": 4, "cpu": 1, "memory_mb": 2048, "disk_gb": 15,
     "template": "base-fedora44-fix"},
    {"name": "db01", "last_octet": 5, "cpu": 1, "memory_mb": 2048, "disk_gb": 15,
     "template": "base-ubuntu24.04-fix"},
]
TEAMS = {"team1": {"identifier": "120", "password": "x"}}
CTX = {
    "teams": TEAMS,
    "boxes_per_team": BOXES,
    "team_passwords": {"team1": "x"},
    "event_name": "t",
    "quotient_admin_password": "x",
}


def _compile(tmp):
    path = Path(tmp) / "packet.yaml"
    path.write_text(PROFILE)
    comp_dir, fidelity, wrote = compile_profile(path, competitions_dir=tmp, force=True)
    return comp_dir, fidelity


class Validation(unittest.TestCase):
    def _errors(self, text):
        import yaml
        return validate_profile(yaml.safe_load(text))

    def test_valid_profile_passes(self):
        self.assertEqual(self._errors(PROFILE), [])

    def test_unknown_pin_rejected(self):
        bad = PROFILE.replace("pin: apache", "pin: apaache")
        errs = self._errors(bad)
        self.assertTrue(any("no Quotient check mapping" in e for e in errs))

    def test_score_only_check_must_resolve(self):
        bad = PROFILE.replace("pin: score/tcp", "pin: score/banana")
        errs = self._errors(bad)
        self.assertTrue(any("score-only pin" in e for e in errs))

    def test_duplicate_display_rejected(self):
        bad = PROFILE.replace("pin: apache, display: http", "pin: apache, display: ssh")
        errs = self._errors(bad)
        self.assertTrue(any("unique <box>-<Display>" in e for e in errs))

    def test_dual_credit_without_domain_credlist_rejected(self):
        bad = PROFILE.replace("    domain:\n      blueteam: \"n0t_s!\"\n", "")
        bad = PROFILE.replace("    domain:\n      blueteam: \"n0t_sus!\"\n", "")
        errs = self._errors(bad)
        self.assertTrue(any("dual_credit needs" in e for e in errs))

    def test_two_dcs_rejected(self):
        bad = PROFILE.replace(
            "domain_role: dc, fidelity: substituted}",
            "domain_role: dc, fidelity: substituted}", 1)
        bad = bad.replace(
            "- {name: web01, packet_os: \"CentOS 8\"",
            "- {name: web01, packet_os: \"CentOS 8\", domain_role: dc", 1)
        errs = self._errors(bad)
        self.assertTrue(any("exactly one DC" in e for e in errs))

    def test_unmanaged_with_service_rejected(self):
        bad = PROFILE.replace(
            "- {box: ad01, name: LDAP",
            "- {box: fw01, name: rogue, port: 80, pin: apache, display: x, fidelity: exact}\n  - {box: ad01, name: LDAP")
        errs = self._errors(bad)
        self.assertTrue(any("unmanaged" in e for e in errs))


class Compile(unittest.TestCase):
    def test_bundle_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir, fidelity = _compile(tmp)
            compfile = (comp_dir / "Compfile").read_text()
            self.assertIn("name Packet Test", compfile)
            self.assertIn("domain_prefix mira-", compfile)
            self.assertIn("domain_suffix .corp.sus", compfile)
            self.assertIn("packet_source", compfile)
            boxes = json.loads((comp_dir / "boxes.json").read_text())
            self.assertEqual([b["name"] for b in boxes],
                             ["fw01", "ad01", "web01", "db01"])
            self.assertTrue(boxes[0]["unmanaged"])
            self.assertNotIn("packet_os", boxes[1])
            services = json.loads((comp_dir / "box_services.json").read_text())
            # ports ride the pins as explicit overrides — packet fidelity, not defaults
            self.assertEqual(services["ad01"][0],
                             {"name": "ADDS", "display": "ldap", "port": 389})
            self.assertEqual(services["ad01"][1],
                             {"name": "score/tcp", "score_only": True, "check": "Tcp",
                              "display": "dns", "port": 53})
            web = services["web01"]
            self.assertEqual(web[0], {"name": "apache", "display": "http", "port": 80})
            self.assertEqual(web[1], {"name": "ssh", "display": "ssh", "port": 22})
            self.assertEqual(web[2], {"name": "ssh", "credlist": "domain",
                                      "display": "ssh-domain", "port": 22})
            users = json.loads((comp_dir / "users.json").read_text())
            self.assertEqual(users["box_username"], "blueteam")
            self.assertEqual(users["credlist_usernames"], ["blueteam", "airship"])
            pw = json.loads((comp_dir / "passwords.json").read_text())
            self.assertEqual(pw["box_password"], "n0t_sus1")
            self.assertEqual(pw["credlists"]["domain"], {"blueteam": "n0t_sus!"})
            roles = json.loads((comp_dir / "domain_roles.json").read_text())
            self.assertEqual(roles, {"ad01": "dc"})
            accounts = json.loads((comp_dir / "domain_accounts.json").read_text())
            self.assertEqual(accounts["accounts"][0]["username"], "blueteam")
            inj = json.loads((comp_dir / "injects" / "01-welcome" / "inject.json").read_text())
            self.assertEqual(inj["description_file"], "briefing.md")
            self.assertTrue((comp_dir / "injects" / "01-welcome" / "briefing.md").exists())
            self.assertIn("score-only tcp", fidelity)
            self.assertIn("domain-credlist twin", fidelity)
            # secrets are 0600
            for secret in ("passwords.json", "domain_accounts.json", "box_baseline.json"):
                self.assertEqual((comp_dir / secret).stat().st_mode & 0o777, 0o600, secret)

    def test_existing_bundle_refused_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir, _ = _compile(tmp)
            with self.assertRaises(SystemExit):
                compile_profile(Path(tmp) / "packet.yaml", competitions_dir=tmp)
            comp_dir2, _, _ = compile_profile(Path(tmp) / "packet.yaml",
                                              competitions_dir=tmp, force=True)
            self.assertEqual(comp_dir, comp_dir2)


class ScoreOnlyAndCredlistPins(unittest.TestCase):
    SERVICES = {
        "ad01": [
            {"name": "ADDS", "display": "ldap"},
            {"name": "score/tcp", "score_only": True, "check": "Tcp",
             "display": "dns", "port": 53},
        ],
        "web01": [
            {"name": "apache", "display": "http"},
            {"name": "ssh", "display": "ssh"},
            {"name": "ssh", "credlist": "domain", "display": "ssh-domain"},
        ],
    }

    def test_event_conf_emits_score_only_and_both_credlists(self):
        conf = build_event_conf(CTX, self.SERVICES)
        by_box = {b["name"]: b for b in conf["box"]}
        self.assertEqual([c["Display"] for c in by_box["ad01"]["Tcp"]], ["ldap", "dns"])
        self.assertEqual([c["Port"] for c in by_box["ad01"]["Tcp"]], [389, 53])
        ssh = by_box["web01"]["Ssh"]
        self.assertEqual([c["Display"] for c in ssh], ["ssh", "ssh-domain"])
        self.assertEqual(ssh[0]["CredLists"], ["linux.credlist"])
        self.assertEqual(ssh[1]["CredLists"], ["domain.credlist"])
        lists = {c["CredlistName"] for c in conf["CredlistSettings"]["Credlist"]}
        self.assertEqual(lists, {"linux.credlist", "domain.credlist"})

    def test_score_only_pin_needs_port(self):
        with self.assertRaises(SystemExit):
            build_event_conf(CTX, {"ad01": [
                {"name": "score/tcp", "score_only": True, "display": "x"}]})

    def test_credlist_override_on_non_cred_check_rejected(self):
        with self.assertRaises(SystemExit):
            build_event_conf(CTX, {"web01": [
                {"name": "apache", "credlist": "domain"}]})

    def test_expected_names_include_score_only(self):
        names = expected_service_names(self.SERVICES, BOXES)
        self.assertEqual(names, {"ad01-ldap", "ad01-dns", "web01-http",
                                 "web01-ssh", "web01-ssh-domain"})

    def test_nakon_config_strips_score_only_keeps_baseline_and_iis_ftp(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "box_services.json").write_text(json.dumps({
                "ad01": self.SERVICES["ad01"],
                "web01": ["IIS FTP"],
                "db01": [],
            }))
            (d / "box_vulns.json").write_text(json.dumps({}))
            (d / "box_baseline.json").write_text(json.dumps({
                "db01": [{"name": "local-user", "vars": {"USERNAME": "scorebot",
                                                         "PASSWORD": "x"}}],
            }))
            path = nakon_ops.generate_nakon_config(TEAMS, BOXES, 1, d, "pw",
                                                   box_username="blueteam")
            cfg = json.loads(Path(path).read_text())
            by_machine = {m["name"]: m for m in cfg["machines"]}
            # fw01 (unmanaged) absent; score-only pin stripped from ad01
            self.assertNotIn("fw01-team120", by_machine)
            ad = by_machine["ad01-team120"]["configurations"]
            # score-only stripped (no plant) AND ADDS stripped (domain_ops re-injects
            # it per team with real vars — DOMAIN_INFRA_CONFIGS)
            self.assertEqual(ad, [])
            self.assertEqual(by_machine["web01-team120"]["configurations"], ["IIS FTP"])
            db = by_machine["db01-team120"]["configurations"]
            self.assertEqual([c["name"] if isinstance(c, dict) else c for c in db],
                             ["local-user"])
            # write-back preserved the score-only pin for push_event_conf
            written = json.loads((d / "box_services.json").read_text())
            self.assertIn("score_only", written["ad01"][1])


class BaselineAccounts(unittest.TestCase):
    def test_out_of_scope_on_all_managed_boxes_windows_credlist_users(self):
        profile = load_profile(Path(_REPO) / "packets/cde-2026/packet.yaml")
        baseline = build_baseline(profile)
        # fw01 unmanaged -> excluded
        self.assertNotIn("fw01", baseline)
        for linux_box in ("web01", "db01"):
            pins = baseline[linux_box]
            names = [p["vars"]["USERNAME"] for p in pins]
            self.assertEqual(sorted(names), ["blackteam", "red_scoring", "scorebot"])
            for p in pins:
                self.assertEqual(p["name"], "local-user")
                self.assertTrue(p["vars"]["PASSWORD"])
        # windows boxes with a cred-carrying service get credlist users whose packet
        # password meets the Windows minimum length (8) — airship/airship is filtered
        # out (live-found 2026-09-30: InvalidPasswordException; it stays MySQL-only)
        ftp_pins = baseline["ftp01"]
        ftp_names = [p["vars"]["USERNAME"] for p in ftp_pins]
        for user in ("scorebot", "blackteam", "red_scoring", "blueteam"):
            self.assertIn(user, ftp_names)
            self.assertEqual(next(p for p in ftp_pins
                                  if p["vars"]["USERNAME"] == user)["name"],
                             "local-user-win")
        self.assertNotIn("airship", ftp_names)
        # credlist users carry the PACKET password verbatim
        self.assertEqual(next(p for p in ftp_pins
                              if p["vars"]["USERNAME"] == "blueteam")["vars"]["PASSWORD"],
                         "n0t_sus1")
        # ad01's checks are all port-open -> decoys only, no credlist pins
        ad_names = [p["vars"]["USERNAME"] for p in baseline["ad01"]]
        self.assertEqual(sorted(ad_names), ["blackteam", "red_scoring", "scorebot"])

    def test_decoy_passwords_are_random_per_compile(self):
        profile = load_profile(Path(_REPO) / "packets/cde-2026/packet.yaml")
        b1 = build_baseline(profile)
        b2 = build_baseline(profile)
        pw = lambda b, box, user: next(p for p in b[box]
                                       if p["vars"]["USERNAME"] == user)["vars"]["PASSWORD"]
        self.assertNotEqual(pw(b1, "web01", "scorebot"), pw(b2, "web01", "scorebot"))
        # credlist users carry the PACKET password verbatim, not a decoy random
        self.assertEqual(pw(b1, "ftp01", "blueteam"), "n0t_sus1")


class DomainKnob(unittest.TestCase):
    def test_template_split(self):
        self.assertEqual(team_domain_parts("mira-{team}.corp.sus"), ("mira-", ".corp.sus"))
        self.assertEqual(team_domain_parts("team{team}.local"), ("team", ".local"))
        with self.assertRaises(SystemExit):
            team_domain_parts("no-placeholder.local")

    def test_team_domain_reads_compfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "Compfile").write_text("name x\nscenario y\ndifficulty 4\n"
                                        "domain_prefix mira-\ndomain_suffix .corp.sus\n")
            self.assertEqual(team_domain(d, 7), "mira-7.corp.sus")
            (d / "Compfile").write_text("name x\nscenario y\ndifficulty 4\n")
            self.assertEqual(team_domain(d, 7), "team7.local")


class CatalogCheckFilter(unittest.TestCase):
    def test_filter_when_score_only_or_baseline_present(self):
        from config_ops import _catalog_check_paths
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "box_services.json").write_text(json.dumps(
                {"ad01": [{"name": "score/tcp", "score_only": True, "display": "dns",
                           "port": 53}, "ADDS"]}))
            (d / "box_vulns.json").write_text(json.dumps({"ad01": ["weak-x"]}))
            (d / "box_baseline.json").write_text(json.dumps(
                {"ad01": ["local-user"]}))
            svc, vuln, cleanup = _catalog_check_paths(d)
            self.assertIsNotNone(cleanup)
            self.assertEqual(json.loads(svc.read_text())["ad01"], ["ADDS"])
            self.assertEqual(json.loads(vuln.read_text())["ad01"],
                             ["weak-x", "local-user"])
            cleanup()
            self.assertFalse(svc.exists())
            self.assertFalse(vuln.exists())

    def test_passthrough_when_nothing_to_filter(self):
        from config_ops import _catalog_check_paths
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "box_services.json").write_text(json.dumps({"ad01": ["ADDS"]}))
            (d / "box_vulns.json").write_text(json.dumps({"ad01": []}))
            svc, vuln, cleanup = _catalog_check_paths(d)
            self.assertIsNone(cleanup)
            self.assertEqual(svc, d / "box_services.json")


class ShippedProfiles(unittest.TestCase):
    def test_both_shipped_profiles_validate(self):
        for name in ("cde-2026", "maccdc-q-2026"):
            profile = load_profile(_REPO / "packets" / name / "packet.yaml")
            self.assertEqual(validate_profile(profile), [], name)

    def test_cde_fidelity_flags_dual_credit_and_gaps(self):
        profile = load_profile(_REPO / "packets/cde-2026/packet.yaml")
        fidelity = packet_ops.render_fidelity(profile)
        # live validation 2026-09-30: the domain SSH dimension is NOT expressible
        # (local blueteam shadows the domain account; shared credlist can't carry
        # per-team qualified names) — the report must say so honestly
        self.assertIn("NOT expressible", fidelity)
        self.assertIn("run-schedule", fidelity)
        maccdc = packet_ops.render_fidelity(
            load_profile(_REPO / "packets/maccdc-q-2026/packet.yaml"))
        self.assertIn("UNSUPPORTED", maccdc)


if __name__ == "__main__":
    unittest.main()
