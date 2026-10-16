"""bad-auto session isolation — the 2026-10-12 vmid-999 incident's regression tests.

Every tezcatlipoca worktree resolves the SAME sibling bad-auto checkout, and its
deploy identity used to live in that checkout's repo-root files (one config.yaml
+ one .deploy-stamp.json): two concurrent sessions rewrote each other's red01
target and erased each other's destroy ground truth, and a validation deploy
following the shared config recycled vmid 999 and killed another session's
in-flight red01. These pin the fix: per-competition config, per-competition
identity resolution, and preflight gates on red01's vmid/IP.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from red import badauto_config_ops as bco  # noqa: E402
from red import red_plant_ops as rp  # noqa: E402


def _comp(d, extra=""):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Compfile").write_text(f"name t\n{extra}")
    return d


class Red01Resolution(unittest.TestCase):
    def test_vmid_derives_from_the_scoring_vmids_block(self):
        # One golden stride past the last band this placement uses: +160 with no
        # satellites, +10 per additional slot. The old +170 landed on slot 2's box 0.
        self.assertEqual(bco.red01_vmid_for(_comp("/tmp/x"), 1300), 1460)
        self.assertEqual(bco.red01_vmid_for(_comp("/tmp/x"), 1310), 1470)
        self.assertEqual(bco.default_red_vmid(1300, 4), 1500)

    def test_a_recorded_placement_beats_the_derivation(self):
        placement = {"red01": {"vmid": 2460}}
        self.assertEqual(bco.red01_vmid_for(_comp("/tmp/x"), 1300, placement), 2460)

    def test_compfile_knob_wins_over_derivation(self):
        d = _comp("/tmp/x", "red01_vmid 777\n")
        self.assertEqual(bco.red01_vmid_for(d, 1300), 777)

    def test_comp_config_is_the_middle_source(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = _comp(td)
            bco.write_config(d, {"deploy": {"red_vmid": 555}})
            self.assertEqual(bco.red01_vmid_for(d, 1300), 555)

    def test_no_sources_is_none_never_999(self):
        """The 2026-10-12 kill: no resolution may land on a shared default."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(bco.red01_vmid_for(_comp(td), None))

    def test_ip_knob_legacy_knob_default(self):
        # No override is UNRESOLVED (''), not a silent legacy default: the address is
        # selected at plant time.
        self.assertEqual(bco.red01_ip_for(_comp("/tmp/x")), "")
        self.assertEqual(bco.red01_ip_for(_comp("/tmp/x", "red01_ip 10.0.0.197\n")),
                         "10.0.0.197")
        self.assertEqual(bco.red01_ip_for(_comp("/tmp/x", "assume_breach_red_ip 10.0.0.196\n")),
                         "10.0.0.196")
        self.assertEqual(bco.red01_ip_for(_comp("/tmp/x", "red01_ip 10.0.0.197\n"
                                                "assume_breach_red_ip 10.0.0.196\n")),
                         "10.0.0.197")  # the new knob wins over the legacy one


class PerCompConfig(unittest.TestCase):
    def test_two_sessions_configs_do_not_clobber(self):
        """THE clobber regression: session A's identity must survive session B's
        staging — two files, each naming its own deploy."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            a = _comp(Path(td) / "iso-a", "red01_ip 10.0.0.240\n")
            b = _comp(Path(td) / "iso-b", "red01_ip 10.0.0.241\n")
            bco.prepare_deploy_config(a, 1300)
            bco.prepare_deploy_config(b, 1310)
            self.assertEqual(bco.load_config(a)["deploy"]["red_vmid"], 1460)
            self.assertEqual(bco.load_config(b)["deploy"]["red_vmid"], 1470)
            self.assertEqual(rp._configured_red_vmid(a), 1460)
            self.assertEqual(rp._configured_red_vmid(b), 1470)

    def test_prepare_carries_blackbox_and_injects_engine_ip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = _comp(td, "red01_ip 10.0.0.240\n")
            bco.write_config(d, {"realm": {"enabled": True, "c2_port": 8000},
                                 "beacon": {"port": 4470},
                                 "deploy": {"template": "base-ubuntu24.04-fix"}})
            path, vmid, ip = bco.prepare_deploy_config(d, 1300, engine_ip="10.0.0.252")
            cfg = bco.load_config(d)
            self.assertEqual((vmid, ip), (1460, "10.0.0.240"))
            self.assertEqual(cfg["competition_dir"], str(d.resolve()))
            self.assertEqual(cfg["deploy"]["engine_ip"], "10.0.0.252")
            self.assertEqual(cfg["deploy"]["template"], "base-ubuntu24.04-fix")  # carried
            self.assertEqual(cfg["realm"]["c2_port"], 8000)                      # carried
            self.assertEqual(cfg["beacon"]["port"], 4470)                        # carried
            self.assertEqual(cfg["deploy"]["red_vmid"], 1460)                    # reserved

    def test_prepare_pins_the_deploys_own_ssh_key(self):
        """bad-auto defaults deploy.ssh_key to a SIBLING tezcatlipoca checkout's `proxmox`
        and reads `<key>.pub` beside it — absent under the dashboard, where the assume-breach
        red01 deploy died on FileNotFoundError after the range itself was built (2026-10-14)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = _comp(td, "red01_ip 10.0.0.240\n")
            key = Path(td) / "keys" / "proxmox"
            key.parent.mkdir()
            key.write_text("PRIVATE")
            bco.prepare_deploy_config(d, 1300, ssh_key=str(key))
            self.assertEqual(bco.load_config(d)["deploy"]["ssh_key"], str(key.resolve()))

    def test_destroy_threads_the_comp_config_and_scrubs_stale_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = _comp(td)
            bco.write_config(d, {"deploy": {"red_vmid": 1470}})
            with mock.patch.object(rp.subprocess, "run") as run:
                rp.destroy_red(d, env={"BAuto_CONFIG": "/stale/config.yaml",
                                       "BAuto_COMPETITION_DIR": str(d)})
            cmd = run.call_args[0][0]
            self.assertEqual(cmd[cmd.index("--config") + 1],
                             str(bco.config_path(d)))
            env = run.call_args[1]["env"]
            self.assertNotIn("BAuto_CONFIG", env)

    def test_destroy_without_a_comp_config_stays_on_the_conventional_flow(self):
        """Pre-isolation comps (and hand-run destroys) have no comp config: never
        pass a missing --config — bad-auto would silently deploy on DEFAULTS."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = _comp(td)
            with mock.patch.object(rp.subprocess, "run") as run:
                rp.destroy_red(d, env={})
            cmd = run.call_args[0][0]
            self.assertNotIn("--config", cmd)

    def test_state_records_the_reserved_vmid(self):
        """plant_assume_breach records vmid in the deploy state and in FAILED
        attempts — the candidate red_vmid_candidates reads (previously
        dead-by-omission, so the state leg of the vmid proof was always empty)."""
        import tempfile
        from tests.test_red_plant_ops import FakeCtx, _CP
        with tempfile.TemporaryDirectory() as td:
            d = _comp(td, "assume_breach 1\nrealm_c2_local 0\n")
            ctx = FakeCtx(d, engine_vmid=1300)
            with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
                 mock.patch.object(rp, "_deploy_red", return_value=_CP(0)), \
                 mock.patch.object(rp, "_seed_red", return_value=_CP(0)), \
                 mock.patch.object(rp, "select_red_ip_for",
                                   return_value=("10.0.0.240", [])):
                out = rp.plant_assume_breach(ctx)
            self.assertTrue(out["ok"])
            self.assertEqual(out["vmid"], 1460)
            self.assertEqual(ctx.state["assume_breach"]["vmid"], 1460)
            self.assertEqual(rp.red_vmid_candidates(d, ctx.state), [1460, 1460])

            ctx2 = FakeCtx(d, engine_vmid=1300)
            with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
                 mock.patch.object(rp, "_deploy_red", return_value=_CP(1, err="boom")), \
                 mock.patch.object(rp, "select_red_ip_for",
                                   return_value=("10.0.0.240", [])):
                rp.plant_assume_breach(ctx2)
            self.assertEqual(ctx2.state["assume_breach"]["vmid"], 1460)


if __name__ == "__main__":
    unittest.main()
