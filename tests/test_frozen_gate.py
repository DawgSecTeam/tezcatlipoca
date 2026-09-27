"""Frozen-gate machinery: per-box payload_hash selectivity, golden_freeze_gate
(config drift refuses naming fields; code drift warns), the pre-migration
bundle_id compat path, and engine hash input classes (incl. the narrowed
scoring_engine terraform block). Pure functions; no API calls."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import template_ops

MANIFEST = {
    "bundle_id": "bundle-v1",
    "inventory": [
        {"name": "web01-golden", "request_key": "rk-web01"},
        {"name": "app01-golden", "request_key": "rk-app01"},
    ],
    "plans": {
        "rk-web01": {"steps": [{"script_sha256": "aaa"}, {"script_sha256": "bbb"}]},
        "rk-app01": {"steps": [{"script_sha256": "ccc"}]},
    },
}


def _bundle_dir(manifest=MANIFEST):
    tmp = tempfile.TemporaryDirectory()
    bundle = Path(tmp.name)
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return tmp, bundle


class PayloadHashSelectivityTests(unittest.TestCase):
    def test_web01_change_moves_only_web01_hash(self):
        tmp, bundle = _bundle_dir()
        self.addCleanup(tmp.cleanup)
        before_web = template_ops.golden_payload_hash(bundle, "web01")
        before_app = template_ops.golden_payload_hash(bundle, "app01")
        changed = json.loads(json.dumps(MANIFEST))
        changed["plans"]["rk-web01"]["steps"][1]["script_sha256"] = "ddd"
        tmp2, bundle2 = _bundle_dir(changed)
        self.addCleanup(tmp2.cleanup)
        after_web = template_ops.golden_payload_hash(bundle2, "web01")
        after_app = template_ops.golden_payload_hash(bundle2, "app01")
        self.assertNotEqual(before_web, after_web)
        self.assertEqual(before_app, after_app)

    def test_missing_box_inventory_returns_none(self):
        tmp, bundle = _bundle_dir()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(template_ops.golden_payload_hash(bundle, "win01"))


def _inputs(payload_hash="p1", golden_configs="g1", code="c1", bundle_id=None):
    config = {"payload_hash": payload_hash, "golden_configs": golden_configs}
    if bundle_id is not None:
        config = {"bundle_id": bundle_id, "golden_configs": golden_configs}
    return {"config": config, "code": {"build_golden_set+apt_prep": code}}


class GoldenFreezeGateTests(unittest.TestCase):
    def _run(self, stored, current, manifest=MANIFEST):
        tmp, bundle_path = _bundle_dir(manifest)
        self.addCleanup(tmp.cleanup)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            template_ops.golden_freeze_gate(
                "web01", stored, current, "2026-09-26", bundle_path)
        return stdout.getvalue()

    def test_clean_match_is_silent(self):
        self.assertEqual(self._run(_inputs(), _inputs()), "")

    def test_config_drift_raises_naming_fields(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(_inputs(payload_hash="old"), _inputs(payload_hash="new"))
        self.assertIn("payload_hash", str(cm.exception))
        self.assertIn("nothing was destroyed", str(cm.exception))

    def test_code_drift_warns_and_proceeds(self):
        out = self._run(_inputs(code="old"), _inputs(code="new"))
        self.assertIn("WARNING", out)
        self.assertIn("build_golden_set+apt_prep", out)

    def test_bundle_id_compat_accepts_unchanged_bundle(self):
        stored = _inputs(bundle_id="bundle-v1")
        current = _inputs(payload_hash="p1")
        self.assertEqual(self._run(stored, current), "")

    def test_bundle_id_compat_rejects_changed_bundle(self):
        stored = _inputs(bundle_id="bundle-old")
        current = _inputs(payload_hash="p1")
        with self.assertRaises(SystemExit) as cm:
            self._run(stored, current)
        self.assertIn("bundle_id", str(cm.exception))
        self.assertIn("payload_hash", str(cm.exception))

    def test_bundle_id_compat_still_fails_on_real_config_drift(self):
        stored = _inputs(bundle_id="bundle-v1", golden_configs="old")
        current = _inputs(payload_hash="p1", golden_configs="new")
        with self.assertRaises(SystemExit) as cm:
            self._run(stored, current)
        self.assertIn("golden_configs", str(cm.exception))
        self.assertNotIn("bundle_id", str(cm.exception))


def _boot_v1():
    return "bootstrap one"


def _boot_v2():
    return "bootstrap two"

_ENGINE_TF = '''
resource "proxmox_virtual_environment_vm" "scoring_engine" {
  vm_id = 1000
}

resource "proxmox_virtual_environment_vm" "team_box" {
  vm_id = 1240
}
'''


class EngineHashInputTests(unittest.TestCase):
    def _inputs(self, vmid=955, ref="v1", tf=_ENGINE_TF, boot=_boot_v1):
        return template_ops.engine_hash_inputs(vmid, ref, tf, boot)

    def test_unchanged_inputs_match(self):
        self.assertEqual(template_ops.classify_drift(self._inputs(), self._inputs()),
                         {"config": [], "code": []})

    def test_unrelated_tf_edit_is_not_engine_drift(self):
        other = _ENGINE_TF.replace("vm_id = 1240", "vm_id = 1241")
        self.assertEqual(template_ops.classify_drift(self._inputs(), self._inputs(tf=other)),
                         {"config": [], "code": []})

    def test_engine_block_edit_is_code_drift(self):
        other = _ENGINE_TF.replace("vm_id = 1000", "vm_id = 1001")
        drift = template_ops.classify_drift(self._inputs(), self._inputs(tf=other))
        self.assertEqual(drift["config"], [])
        self.assertEqual(drift["code"], ["main.tf:scoring_engine"])

    def test_bootstrap_edit_is_code_drift(self):
        drift = template_ops.classify_drift(self._inputs(), self._inputs(boot=_boot_v2))
        self.assertEqual(drift["code"], ["bootstrap_scoring_engine"])

    def test_base_vmid_edit_is_config_drift(self):
        drift = template_ops.classify_drift(self._inputs(), self._inputs(vmid=956))
        self.assertEqual(drift["config"], ["base_engine_vm_id"])
        self.assertEqual(drift["code"], [])


if __name__ == "__main__":
    unittest.main()
