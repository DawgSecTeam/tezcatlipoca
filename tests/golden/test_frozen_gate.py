"""Frozen-gate machinery: per-box payload_hash selectivity, golden_freeze_gate
(config drift refuses naming fields; code drift warns), and engine hash input classes. Pure
functions; no API calls."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from engine import template_freeze

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
        before_web = template_freeze.golden_payload_hash(bundle, "web01")
        before_app = template_freeze.golden_payload_hash(bundle, "app01")
        changed = json.loads(json.dumps(MANIFEST))
        changed["plans"]["rk-web01"]["steps"][1]["script_sha256"] = "ddd"
        tmp2, bundle2 = _bundle_dir(changed)
        self.addCleanup(tmp2.cleanup)
        after_web = template_freeze.golden_payload_hash(bundle2, "web01")
        after_app = template_freeze.golden_payload_hash(bundle2, "app01")
        self.assertNotEqual(before_web, after_web)
        self.assertEqual(before_app, after_app)



def _inputs(payload_hash="p1", golden_configs="g1", code="c1"):
    config = {"payload_hash": payload_hash, "golden_configs": golden_configs}
    return {"config": config, "code": {"build_golden_set+apt_prep": code}}


class GoldenFreezeGateTests(unittest.TestCase):
    def _run(self, stored, current):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            template_freeze.golden_freeze_gate("web01", stored, current, "2026-01-01")
        return stdout.getvalue()


    def test_config_drift_raises_naming_fields(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(_inputs(payload_hash="old"), _inputs(payload_hash="new"))
        self.assertIn("payload_hash", str(cm.exception))
        self.assertIn("nothing was destroyed", str(cm.exception))




def _boot_v1():
    return "bootstrap one"


def _boot_v2():
    return "bootstrap two"


def _prose_only():
    """A docstring that must not reach the hash."""
    # a comment that must not reach the hash either
    return 7


def _different_value():
    return 8

class EngineHashInputTests(unittest.TestCase):
    def _inputs(self, vmid=955, ref="v1", boot=_boot_v1):
        return template_freeze.engine_hash_inputs(vmid, ref, boot)

    def test_bootstrap_edit_is_code_drift(self):
        drift = template_freeze.classify_drift(self._inputs(), self._inputs(boot=_boot_v2))
        self.assertEqual(drift["config"], [])
        self.assertEqual(drift["code"], ["bootstrap_scoring_engine"])

    def test_comments_and_docstrings_are_stripped_from_the_hash(self):
        # The hash exists to catch changes that alter the disk; a prose edit must not rebuild
        # a 40 GB template for an hour.
        normalized = template_freeze._normalized_source(_prose_only)
        self.assertNotIn("#", normalized)
        self.assertNotIn("A docstring", normalized)
        self.assertIn("return 7", normalized)
        self.assertNotEqual(template_freeze.code_hash(_prose_only),
                            template_freeze.code_hash(_different_value))

    def test_the_pinned_quotient_ref_is_config_drift(self):
        drift = template_freeze.classify_drift(self._inputs(), self._inputs(ref="v2"))
        self.assertEqual(drift["config"], ["quotient_ref"])


if __name__ == "__main__":
    unittest.main()
