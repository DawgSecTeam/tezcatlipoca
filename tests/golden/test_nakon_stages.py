"""Three-stage split safety: the golden identity ban on BOTH golden paths (slot 0 and
every satellite) and the combined post-clone pin merge.

Offline — generate_stage_configs / generate_slot_golden_config run against a written
nakon-config.json (Proxmox/Nakon boundaries are upstream)."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from common import constants
from nakonops import nakon_config_ops
from nakonops import nakon_pin_ops

BOXES = [
    {"name": "web01", "last_octet": 4, "template": "base-fedora44-fix"},
    {"name": "db01", "last_octet": 5, "template": "base-ubuntu24.04-fix"},
]
TEAMS = {"team1": {"identifier": 104}, "team2": {"identifier": 105}}

# A fabricated config that requires ONLY a cross-box identity var and is NOT in
# POST_CLONE_CONFIGS. This is the shape the old satellite path let through: every real
# "ip:<box>" config currently also rides PREDOMAIN_STAGE_CONFIGS, which masked the bug.
CROSSBOX_ONLY = {"hypothetical-crossbox": {"DB_HOST": "ip:db01"}}


def _write_crossbox(comp_dir, config_name="hypothetical-crossbox"):
    machines = []
    for team, ident in (("team1", 104), ("team2", 105)):
        machines.append({"name": f"web01-{team}", "ip": f"192.168.{ident}.4",
                         "configurations": [{"name": config_name,
                                             "vars": {"DB_HOST": f"192.168.{ident}.5"}}]})
        machines.append({"name": f"db01-{team}", "ip": f"192.168.{ident}.5",
                         "configurations": []})
    (comp_dir / "nakon-config.json").write_text(json.dumps({"machines": machines}))


# The satellite golden enforced a strictly weaker ban than slot 0, so a
# cross-box-only ("ip:<box>") config rode the satellite disk with team1's DB IP
# already baked into the pin.
class GoldenIdentityBan(unittest.TestCase):
    def test_slot0_golden_rejects_crossbox_only_identity_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            _write_crossbox(comp_dir)
            with patch.dict(constants.REQUIRED_VARS, CROSSBOX_ONLY):
                with self.assertRaises(SystemExit) as cm:
                    nakon_config_ops.generate_stage_configs(comp_dir, TEAMS, BOXES)
            self.assertIn("hypothetical-crossbox", str(cm.exception))
            self.assertFalse((comp_dir / ".nakon-golden.json").exists())

    def test_satellite_golden_rejects_crossbox_only_identity_config(self):
        # Before the fix this path used exact "ip" matching (REQUIRED_VARS values),
        # so it WROTE .nakon-golden-slot2.json with the config aboard — every clone
        # on the satellite would have pointed at team1's database.
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            _write_crossbox(comp_dir)
            with patch.dict(constants.REQUIRED_VARS, CROSSBOX_ONLY):
                with self.assertRaises(SystemExit) as cm:
                    nakon_config_ops.generate_slot_golden_config(
                        comp_dir, BOXES, frozenset(), 105, 2)
            self.assertIn("hypothetical-crossbox", str(cm.exception))
            self.assertFalse((comp_dir / ".nakon-golden-slot2.json").exists())




# The combined pre-domain ∪ final view deduped by config name only, silently
# dropping same-named pins that differ by vars.
class PostCloneMerge(unittest.TestCase):
    COLD_BOXES = [{"name": "dc01", "template": "windows-server-2022"}]
    COLD_TEAMS = {"team1": {"identifier": 104}}

    @staticmethod
    def _write(comp_dir, configurations):
        (comp_dir / "nakon-config.json").write_text(json.dumps({"machines": [
            {"name": "dc01-team1", "ip": "192.168.104.6",
             "configurations": configurations}]}))

    @staticmethod
    def _pins(path, machine="dc01-team1"):
        machines = json.loads(Path(path).read_text())["machines"]
        entry = next(m for m in machines if m["name"] == machine)
        return [(c, {}) if isinstance(c, str) else (c["name"], c.get("vars") or {})
                for c in entry["configurations"]]

    def test_postclone_keeps_same_named_decoys_on_cold_box(self):
        # A DC's golden is unbooted, so its otherwise golden-stage configs enter the
        # pre-domain stage. box_baseline.json ships several same-named local-user decoys;
        # the name-only merge kept only the first (postclone: 2 pins of 4).
        decoys = [
            {"name": "local-user", "vars": {"USERNAME": "scorebot", "PASSWORD": "a"}},
            {"name": "local-user", "vars": {"USERNAME": "blackteam", "PASSWORD": "b"}},
            {"name": "local-user", "vars": {"USERNAME": "red_scoring", "PASSWORD": "c"}},
            "install-smb-v1",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            self._write(comp_dir, copy.deepcopy(decoys))
            _, predomain_p, _, postclone_p, _ = nakon_config_ops.generate_stage_configs(
                comp_dir, self.COLD_TEAMS, self.COLD_BOXES, unbooted={"dc01"})
            predomain = self._pins(predomain_p)
            postclone = self._pins(postclone_p)
        self.assertEqual([n for n, _ in postclone],
                         ["local-user", "local-user", "local-user", "install-smb-v1"])
        self.assertEqual([v.get("USERNAME") for n, v in postclone if n == "local-user"],
                         ["scorebot", "blackteam", "red_scoring"])
        self.assertEqual(postclone, predomain, "the merge must not lose pre-domain pins")



class AccountFileModeStaging(unittest.TestCase):
    """An account-file MODE must plant in the deploy's LAST pass.

    shadow-utils' commonio (useradd/groupadd) recreates /etc/passwd, /etc/shadow and
    /etc/group at their default modes — probed in the Debian 13 and Ubuntu 24.04
    base images: `chmod 666` then `useradd` reads 664, while `chpasswd` alone preserves it.
    Two account writers run after the pre-domain and final passes: fix_services' credlist
    `useradd` (end of phase 6) and bad-auto's assume-breach seed (persistence `user`,
    phase 7). A mode staged any earlier is gone before verify probes it — caught live:
    bad-perms-userConfig read ABSENT on every team clone while writable-sudoers, a DIRECTORY
    mode neither writer touches, was present on all of them.
    """

    MODE_BOXES = [{"name": "web01", "template": "base-debian13-lite-fix"},
                  {"name": "mail01", "template": "base-ubuntu24.04-fix"}]
    MODE_TEAMS = {"team1": {"identifier": 152}, "team2": {"identifier": 153}}

    @staticmethod
    def _names(path):
        machines = json.loads(Path(path).read_text())["machines"]
        return {m["name"]: [c if isinstance(c, str) else c["name"]
                            for c in m["configurations"]]
                for m in machines}

    def test_every_verifier_mode_probe_on_an_account_file_is_staged_last(self):
        from verifier.misconfig import MISCONFIG_CHECKS

        checked = 0
        for name, (cmd, _predicate) in MISCONFIG_CHECKS.items():
            if "stat -c %a" not in cmd:
                continue
            if not any(p in cmd for p in ("/etc/passwd", "/etc/shadow",
                                          "/etc/group", "/etc/gshadow")):
                continue
            checked += 1
            self.assertIn(name, constants.ACCOUNT_FILE_MODE_CONFIGS)
            self.assertNotIn(name, constants.PREDOMAIN_STAGE_CONFIGS)
            self.assertNotIn(name, constants.FINAL_STAGE_CONFIGS)
        self.assertTrue(checked, "no account-file mode probe is left to guard")

    def test_account_file_modes_plant_only_in_the_last_pass(self):
        # web01 pins the named modes plus a control that must keep riding the golden;
        # mail01 pins the parameterized shape (loosen-file-mode on /etc/shadow), which the
        # name list alone cannot see.
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            machines = []
            for team, ident in (("team1", 152), ("team2", 153)):
                machines.append({"name": f"web01-{team}", "ip": f"192.168.{ident}.4",
                                 "configurations": ["nginx-off-by-slash",
                                                    "bad-perms-userConfig",
                                                    "writable-etc-group"]})
                machines.append({"name": f"mail01-{team}", "ip": f"192.168.{ident}.5",
                                 "configurations": [
                                     {"name": "loosen-file-mode",
                                      "vars": {"FILE_PATH": "/etc/shadow",
                                               "FILE_MODE": "0666"}}]})
            (comp_dir / "nakon-config.json").write_text(json.dumps({"machines": machines}))
            paths = nakon_config_ops.generate_stage_configs(
                comp_dir, self.MODE_TEAMS, self.MODE_BOXES)
            golden, predomain, final, postclone, late = (self._names(p) for p in paths)

        self.assertIn("nginx-off-by-slash", golden["web01-golden"])
        for name in ("bad-perms-userConfig", "writable-etc-group"):
            self.assertNotIn(name, golden["web01-golden"])
        self.assertNotIn("loosen-file-mode", golden["mail01-golden"])

        self.assertEqual(late["web01-team1"],
                         ["bad-perms-userConfig", "writable-etc-group"])
        self.assertEqual(late["web01-team2"],
                         ["bad-perms-userConfig", "writable-etc-group"])
        self.assertEqual(late["mail01-team1"], ["loosen-file-mode"])
        self.assertEqual(late["mail01-team2"], ["loosen-file-mode"])
        for name in ("bad-perms-userConfig", "writable-etc-group", "loosen-file-mode"):
            self.assertNotIn(name, predomain.get("web01-team1", []))
            self.assertNotIn(name, final.get("web01-team1", []))
            # not in the sweep view either: redeploy replays that before hardening, whose
            # credlist useradd would clobber a mode planted there. Redeploy runs the late
            # file itself, after hardening.
            self.assertNotIn(name, postclone.get("web01-team1", []))
            self.assertNotIn(name, postclone.get("mail01-team1", []))


if __name__ == "__main__":
    unittest.main()
