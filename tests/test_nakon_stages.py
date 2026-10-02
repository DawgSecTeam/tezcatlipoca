"""Three-stage split safety: the golden identity ban on BOTH golden paths (slot 0 and
every satellite) and the combined post-clone pin merge.

Offline — generate_stage_configs / generate_slot_golden_config run against a written
nakon-config.json (Proxmox/Nakon boundaries are upstream)."""

import copy
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import nakon_ops

BOXES = [
    {"name": "web01", "last_octet": 4, "template": "base-fedora44-fix"},
    {"name": "db01", "last_octet": 5, "template": "base-ubuntu24.04-fix"},
]
TEAMS = {"team1": {"identifier": 104}, "team2": {"identifier": 105}}

# A fabricated config that requires ONLY a cross-box identity var and is NOT in
# POST_CLONE_CONFIGS. This is the shape the old satellite path let through: every real
# "ip:<box>" config currently also rides REPAIR_STAGE_CONFIGS, which masked the bug.
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


class GoldenIdentityBan(unittest.TestCase):
    """D1: the satellite golden enforced a strictly weaker ban than slot 0, so a
    cross-box-only ("ip:<box>") config rode the satellite disk with team1's DB IP
    already baked into the pin."""

    def test_slot0_golden_rejects_crossbox_only_identity_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            _write_crossbox(comp_dir)
            with patch.dict(nakon_ops.REQUIRED_VARS, CROSSBOX_ONLY):
                with self.assertRaises(SystemExit) as cm:
                    nakon_ops.generate_stage_configs(comp_dir, TEAMS, BOXES)
            self.assertIn("hypothetical-crossbox", str(cm.exception))
            self.assertFalse((comp_dir / ".nakon-golden.json").exists())

    def test_satellite_golden_rejects_crossbox_only_identity_config(self):
        # Before the fix this path used exact "ip" matching (REQUIRED_VARS values),
        # so it WROTE .nakon-golden-slot2.json with the config aboard — every clone
        # on the satellite would have pointed at team1's database.
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            _write_crossbox(comp_dir)
            with patch.dict(nakon_ops.REQUIRED_VARS, CROSSBOX_ONLY):
                with self.assertRaises(SystemExit) as cm:
                    nakon_ops.generate_slot_golden_config(
                        comp_dir, BOXES, frozenset(), 105, 2)
            self.assertIn("hypothetical-crossbox", str(cm.exception))
            self.assertFalse((comp_dir / ".nakon-golden-slot2.json").exists())

    def test_both_paths_share_one_identity_ban_definition(self):
        # The ban is computed inside _golden_stage_machines via
        # _identity_banned_configs, so slot 0 and the satellites cannot diverge.
        with patch.dict(nakon_ops.REQUIRED_VARS, CROSSBOX_ONLY):
            banned = nakon_ops._identity_banned_configs()
        self.assertIn("hypothetical-crossbox", banned)   # ip:<box>
        self.assertIn("hosts-redirect-linux", banned)    # ip
        self.assertIn("airship-webapp", banned)          # ip:polus
        self.assertNotIn("sudoers-rule", banned)         # literal only

    def test_non_identity_config_still_rides_the_satellite_golden(self):
        # The ban must not over-reach: an ordinary config still builds the golden.
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            _write_crossbox(comp_dir, config_name="install-nginx")
            path = nakon_ops.generate_slot_golden_config(
                comp_dir, BOXES, frozenset(), 105, 2)
            names = [(m["name"], [c if isinstance(c, str) else c["name"]
                                  for c in m["configurations"]])
                     for m in json.loads(path.read_text())["machines"]]
        self.assertEqual(names, [("web01-golden", ["install-nginx"]),
                                 ("db01-golden", [])])


class PostCloneMerge(unittest.TestCase):
    """D2: the combined repair ∪ final view deduped by config name only, silently
    dropping same-named pins that differ by vars."""

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
        # repair stage. box_baseline.json ships several same-named local-user decoys;
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
            _, repair_p, _, postclone_p = nakon_ops.generate_stage_configs(
                comp_dir, self.COLD_TEAMS, self.COLD_BOXES, unbooted={"dc01"})
            repair = self._pins(repair_p)
            postclone = self._pins(postclone_p)
        self.assertEqual([n for n, _ in postclone],
                         ["local-user", "local-user", "local-user", "install-smb-v1"])
        self.assertEqual([v.get("USERNAME") for n, v in postclone if n == "local-user"],
                         ["scorebot", "blackteam", "red_scoring"])
        self.assertEqual(postclone, repair, "the merge must not lose repair pins")

    def test_postclone_still_dedups_identical_pin_across_stages(self):
        # The merge exists so a pin in BOTH the repair and final subsets plants once;
        # the vars-aware key must preserve that.
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            self._write(comp_dir, [{"name": "both-stage-x", "vars": {"K": "v"}}])
            with patch.object(nakon_ops, "REPAIR_STAGE_CONFIGS",
                              nakon_ops.REPAIR_STAGE_CONFIGS | {"both-stage-x"}), \
                 patch.object(nakon_ops, "FINAL_STAGE_CONFIGS",
                              nakon_ops.FINAL_STAGE_CONFIGS | {"both-stage-x"}):
                _, _, _, postclone_p = nakon_ops.generate_stage_configs(
                    comp_dir, self.COLD_TEAMS, self.COLD_BOXES)
            postclone = self._pins(postclone_p)
        self.assertEqual(postclone, [("both-stage-x", {"K": "v"})])


class DeadParameters(unittest.TestCase):
    """D4: three functions took a parameter they never used. Removing them is only
    safe because every caller reaches the two public ones positionally/keyword-only
    from files this stream does not own (deploy.py, redeploy-competition.py)."""

    def test_generate_stage_configs_dropped_box_username(self):
        self.assertNotIn("box_username",
                         inspect.signature(nakon_ops.generate_stage_configs).parameters)

    def test_generate_slot_golden_config_dropped_teams(self):
        self.assertNotIn("teams", inspect.signature(
            nakon_ops.generate_slot_golden_config).parameters)

    def test_golden_stage_machines_dropped_boxes(self):
        self.assertNotIn("boxes", inspect.signature(
            nakon_ops._golden_stage_machines).parameters)


if __name__ == "__main__":
    unittest.main()
