"""Phase 5 (in-path firewall bootstrap) driven end to end offline.

Regression: deploy_lib/phases/firewall.py called write_team_configs(comp_dir, teams,
red_dnat_spec=...) while the function demanded an unused positional `fw_box`, so the first
in-path-firewall deploy would have died with a TypeError at phase 5 (the live runs so far
had no in_path box, and every test stubbed the call). The consoles/engine are faked; the
config writer is real."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from deploy_lib.phases import firewall  # noqa: E402

SEED = (Path(__file__).parent / "test_firewall_ops.py").read_text().split('SEED = """')[1].split('"""')[0]
TEAMS = {"team1": {"identifier": "120", "password": "x"},
         "team2": {"identifier": "121", "password": "x"}}
BOXES = [{"name": "fw01", "template": "pfsense", "last_octet": 1, "unmanaged": True,
          "in_path": True},
         {"name": "web01", "template": "base-ubuntu24.04-fix", "last_octet": 2}]


class Phase5Firewall(unittest.TestCase):
    def _ctx(self, comp, from_phase=5, placement=None):
        targets = [{"team_key": k, "box": b, "ip": f"192.168.{t['identifier']}.{b['last_octet']}",
                    "vm_name": f"{b['name']}-{k}", "vmid": 1}
                   for k, t in TEAMS.items() for b in BOXES]
        saved = []
        return SimpleNamespace(
            from_phase=from_phase, all_targets=targets, placement=placement, comp_dir=comp,
            teams=TEAMS, node="pve1", tf_ctx={}, comp_name=comp.name, state={},
            save_state=lambda: saved.append(1))

    def _comp(self, root, with_seed=True):
        comp = Path(root) / "fwcomp"
        (comp / "pfsense").mkdir(parents=True)
        (comp / "Compfile").write_text("name fwcomp\nfirewall_dnat 8080->192.168.{tid}.2\n")
        if with_seed:
            (comp / "pfsense" / "pfsense-config-orig.xml").write_text(SEED)
        return comp

    def test_phase5_writes_configs_and_drives_bootstrap(self):
        with tempfile.TemporaryDirectory() as root:
            comp = self._comp(root)
            ctx = self._ctx(comp)
            with patch.object(firewall, "bootstrap_firewalls") as boot, \
                    patch.object(firewall, "cut_over_engine") as cut, \
                    patch.object(firewall, "verify_in_path") as ver, \
                    patch.object(firewall, "run_concurrent") as conc, \
                    patch("builtins.print"):
                firewall.phase5_firewall_bootstrap(ctx)
            for ident in ("120", "121"):
                self.assertTrue((comp / "pfsense" / f"config-team{ident}.xml").exists())
            paths = boot.call_args.args[2]
            self.assertEqual(set(paths), {"team1", "team2"})
            cut.assert_called_once()
            self.assertEqual(ver.call_args.args[2], {"team1": "192.168.120.2",
                                                     "team2": "192.168.121.2"})
            self.assertTrue(conc.called)
            self.assertTrue(ctx.state["firewalls_bootstrapped"])

    def test_missing_seed_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = self._ctx(self._comp(root, with_seed=False))
            with patch("builtins.print"), self.assertRaises(SystemExit) as cm:
                firewall.phase5_firewall_bootstrap(ctx)
            self.assertIn("pfsense-config-orig.xml", str(cm.exception))

    def test_vyos_lineup_needs_no_seed_and_writes_both_passes(self):
        """The VyOS config is fully generated — the pfSense seed-missing error must not
        fire for a vyos template, and the pass-A/pass-B artifacts both land."""
        with tempfile.TemporaryDirectory() as root:
            comp = self._comp(root, with_seed=False)
            ctx = self._ctx(comp)
            for t in ctx.all_targets:
                if t["box"]["name"] == "fw01":
                    t["box"]["template"] = "vyos-provision"
            with patch.object(firewall, "bootstrap_firewalls") as boot, \
                    patch.object(firewall, "cut_over_engine"), \
                    patch.object(firewall, "verify_in_path") as ver, \
                    patch.object(firewall, "run_concurrent"), \
                    patch("builtins.print"):
                firewall.phase5_firewall_bootstrap(ctx)
            for ident in ("120", "121"):
                a = comp / "vyos" / f"config-team{ident}.cmds"
                b = comp / "vyos" / f"config-team{ident}-lan.cmds"
                self.assertTrue(a.exists() and b.exists())
                self.assertIn("eth0 address 172.31." + ident, a.read_text())
                self.assertIn(f"eth1 address 192.168.{ident}.1/24", b.read_text())
            self.assertFalse((comp / "pfsense").exists() and
                             any((comp / "pfsense").glob("config-team*.xml")))
            self.assertIn("vyos-provision", str(boot.call_args))
            self.assertEqual(ver.call_args.kwargs.get("kind"), "vyos")
            self.assertTrue(ctx.state["firewalls_bootstrapped"])

    def test_skips_without_firewall_or_on_resume_past_5(self):
        with tempfile.TemporaryDirectory() as root:
            comp = self._comp(root)
            ctx = self._ctx(comp)
            ctx.all_targets = [t for t in ctx.all_targets if not t["box"].get("in_path")]
            with patch.object(firewall, "write_team_configs") as w, patch("builtins.print"):
                firewall.phase5_firewall_bootstrap(ctx)
                firewall.phase5_firewall_bootstrap(self._ctx(comp, from_phase=6))
            w.assert_not_called()

    def test_refuses_satellite_placement(self):
        with tempfile.TemporaryDirectory() as root:
            ctx = self._ctx(self._comp(root), placement={"satellites": [{"name": "n2"}]})
            with patch("builtins.print"), self.assertRaises(SystemExit):
                firewall.phase5_firewall_bootstrap(ctx)


if __name__ == "__main__":
    unittest.main()
