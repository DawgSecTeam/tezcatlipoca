"""red_plant_ops — pre-competition assume-breach planting (phase 7).

No live range: subprocess is mocked, so these prove the control flow, the
degradation-not-abort contract, and the Compfile knobs.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import red_plant_ops as rp  # noqa: E402


class FakeCtx:
    def __init__(self, comp_dir, ssh_key="key.pem"):
        self.comp_dir = Path(comp_dir)
        self.ssh_key_abs = ssh_key
        self.ssh_key = ssh_key
        self.state = {}
        self.saved = 0

    def save_state(self):
        self.saved += 1


def _write_compfile(d, extra=""):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Compfile").write_text(f"name t\n{extra}")
    return d


class _CP:
    def __init__(self, returncode=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = returncode, out, err


class PlantAssumeBreachTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="tz-redplant-")

    def test_no_bad_auto_checkout_degrades(self):
        ctx = FakeCtx(_write_compfile(self.tmp))
        with mock.patch.object(rp, "_bad_auto_present", return_value=False), \
             mock.patch.object(rp, "record_degradation") as deg, \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2", return_value=(None, None)):
            out = rp.plant_assume_breach(ctx)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "no bad-auto checkout")
        deg.assert_called_once()

    def test_deploy_failure_degrades_and_does_not_seed(self):
        ctx = FakeCtx(_write_compfile(self.tmp))
        with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
             mock.patch.object(rp, "_deploy_red", return_value=_CP(1, err="boom")) as dep, \
             mock.patch.object(rp, "_seed_red") as seed, \
             mock.patch.object(rp, "record_degradation") as deg, \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2", return_value=(None, None)):
            out = rp.plant_assume_breach(ctx)
        self.assertEqual(out["reason"], "deploy failed")
        dep.assert_called_once()
        seed.assert_not_called()
        deg.assert_called_once()

    def test_success_records_state_and_teardown_hint(self):
        ctx = FakeCtx(_write_compfile(self.tmp, "assume_breach_depth 2\n"))
        with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
             mock.patch.object(rp, "_deploy_red", return_value=_CP(0)), \
             mock.patch.object(rp, "_seed_red", return_value=_CP(0)) as seed, \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2", return_value=(None, None)):
            out = rp.plant_assume_breach(ctx)
        self.assertTrue(out["ok"])
        self.assertEqual(out["depth"], 2)
        self.assertEqual(ctx.state["assume_breach"]["red_ip"], out["red_ip"])
        self.assertEqual(ctx.saved, 1)
        # the seed ran on red01 with the configured depth
        ssh_key, red_ip, depth = seed.call_args[0][:3]
        self.assertEqual(depth, 2)
        self.assertEqual(red_ip, out["red_ip"])

    def test_local_c2_default_on_provisions_between_deploy_and_seed(self):
        """Default knob ON: derived config feeds the deploy, tavern provisioner
        runs after it and BEFORE the seed, and the summary carries the C2."""
        ctx = FakeCtx(_write_compfile(self.tmp))
        order = []
        cfg_path, realm_cfg = Path(self.tmp) / ".realm-c2-config.yaml", {"realm": {"c2_ip": "1.2.3.4"}}
        def _prov(*a, **k):
            order.append("provision")
            return {"ok": True, "transports": ["grpc"], "mcp": "/mcp"}
        with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
             mock.patch.object(rp, "_deploy_red", return_value=_CP(0),
                               side_effect=lambda *a, **k: order.append("deploy") or _CP(0)), \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2",
                               return_value=(cfg_path, realm_cfg)), \
             mock.patch.object(rp.realm_c2_ops, "provision_realm_c2",
                               side_effect=_prov), \
             mock.patch.object(rp, "_seed_red", return_value=_CP(0),
                               side_effect=lambda *a, **k: order.append("seed") or _CP(0)):
            out = rp.plant_assume_breach(ctx)
            self.assertEqual(rp._deploy_red.call_args.kwargs["config_path"], cfg_path)
        self.assertTrue(out["ok"])
        self.assertEqual(order, ["deploy", "provision", "seed"])
        self.assertEqual(out["realm_c2"]["ok"], True)
        self.assertEqual(ctx.state["assume_breach"]["realm_c2"]["ok"], True)

    def test_local_c2_provision_failure_degrades_but_still_seeds(self):
        ctx = FakeCtx(_write_compfile(self.tmp))
        with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
             mock.patch.object(rp, "_deploy_red", return_value=_CP(0)), \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2",
                               return_value=(Path(self.tmp) / "c.yaml", {"realm": {}})), \
             mock.patch.object(rp.realm_c2_ops, "provision_realm_c2",
                               return_value={"ok": False, "error": "build died"}), \
             mock.patch.object(rp, "_seed_red", return_value=_CP(0)) as seed, \
             mock.patch.object(rp, "record_degradation") as deg:
            out = rp.plant_assume_breach(ctx)
        self.assertTrue(out["ok"])                      # degrade, never abort
        self.assertFalse(out["realm_c2"]["ok"])
        seed.assert_called_once()                       # the seed still runs
        deg.assert_called_once()                        # ...and the failure is named
        self.assertEqual(deg.call_args[0][0], "realm_c2_provision")

    def test_local_c2_knob_off_keeps_legacy_layout(self):
        ctx = FakeCtx(_write_compfile(self.tmp, "realm_c2_local 0\n"))
        with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
             mock.patch.object(rp, "_deploy_red", return_value=_CP(0)) as dep, \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2") as prep, \
             mock.patch.object(rp.realm_c2_ops, "provision_realm_c2") as prov, \
             mock.patch.object(rp, "_seed_red", return_value=_CP(0)):
            out = rp.plant_assume_breach(ctx)
        self.assertTrue(out["ok"])
        prep.assert_not_called()
        prov.assert_not_called()
        self.assertIsNone(dep.call_args.kwargs.get("config_path"))
        self.assertNotIn("realm_c2", out)

    def test_seed_failure_degrades(self):
        ctx = FakeCtx(_write_compfile(self.tmp))
        with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
             mock.patch.object(rp, "_deploy_red", return_value=_CP(0)), \
             mock.patch.object(rp, "_seed_red", return_value=_CP(1, err="nope")), \
             mock.patch.object(rp, "record_degradation") as deg, \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2",
                               return_value=(None, None)):
            out = rp.plant_assume_breach(ctx)
        self.assertEqual(out["reason"], "seed failed")
        deg.assert_called_once()
        self.assertNotIn("assume_breach", ctx.state)

    def test_depth_is_clamped(self):
        ctx = FakeCtx(_write_compfile(self.tmp, "assume_breach_depth 9\n"))
        with mock.patch.object(rp, "_bad_auto_present", return_value=True), \
             mock.patch.object(rp, "_deploy_red", return_value=_CP(0)), \
             mock.patch.object(rp, "_seed_red", return_value=_CP(0)), \
             mock.patch.object(rp.realm_c2_ops, "prepare_local_c2",
                               return_value=(None, None)):
            out = rp.plant_assume_breach(ctx)
        self.assertEqual(out["depth"], rp.SEED_DEPTH_MAX)

    def test_compfile_red_ip_override_wins(self):
        d = _write_compfile(self.tmp, "assume_breach_red_ip 10.9.9.9\n")
        self.assertEqual(rp.red_ip_for(d), "10.9.9.9")

    def test_red_ip_falls_back_to_default(self):
        d = _write_compfile(self.tmp)
        with mock.patch.object(rp, "_configured_red_ip", return_value=None):
            self.assertEqual(rp.red_ip_for(d), rp.DEFAULT_RED_IP)


if __name__ == "__main__":
    unittest.main()
