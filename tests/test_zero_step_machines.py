"""Zero-step machine reconciliation in run_nakon (live-found 2026-10-04, fw-live stage A).

An unpinned box in a lineup dispatches an EMPTY plan; nakon's runner reports the silent
no-op as "no output from the remote plan (exit 0) — check credentials and sudo access"
and fails the STRICT golden pass on a genuine no-op (twice, on fresh clones). Offline:
_reconcile_zero_step_machines is pure against a temp config file."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from nakon_ops import _reconcile_zero_step_machines

ZERO_STEP_ERR = "no output from the remote plan (exit 0) \u2014 check credentials and sudo access"


def _cfg(tmp, machines):
    p = Path(tmp) / "golden.json"
    p.write_text(json.dumps({"machines": machines}))
    return p


class ReconcileZeroStep(unittest.TestCase):
    def test_zero_step_machine_is_recorded_clean(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d, [{"name": "db01-golden", "ip": "192.168.140.242",
                            "configurations": []}])
            machines = [{"name": "db01-golden", "ip": "192.168.140.242", "steps": [],
                         "error": ZERO_STEP_ERR, "exit_status": 0}]
            failed = [f"[nakon] db01-golden: FAILED — {ZERO_STEP_ERR}"]
            out, failed = _reconcile_zero_step_machines(machines, cfg, failed)
        self.assertEqual(out[0]["error"], None)
        self.assertEqual(out[0]["steps"], [{"name": "(empty plan)", "rc": 0, "seconds": 0}])
        self.assertEqual(failed, [])

    def test_real_failure_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d, [{"name": "web01-golden", "configurations": ["nginx"]}])
            machines = [{"name": "web01-golden", "steps": [],
                         "error": ZERO_STEP_ERR, "exit_status": 0}]
            failed = ["[nakon] web01-golden: FAILED — no output from the remote plan"]
            out, failed = _reconcile_zero_step_machines(machines, cfg, failed)
        self.assertEqual(out[0]["error"], ZERO_STEP_ERR)  # box HAS pins: real failure
        self.assertEqual(len(failed), 1)

    def test_mixed_plant_reconciles_only_the_empty_one(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d, [{"name": "web01-golden", "configurations": ["nginx"]},
                           {"name": "db01-golden", "configurations": []}])
            machines = [
                {"name": "web01-golden", "steps": [{"name": "nginx", "rc": 0}],
                 "error": None, "exit_status": 0},
                {"name": "db01-golden", "steps": [], "error": ZERO_STEP_ERR,
                 "exit_status": 0},
            ]
            failed = ["[nakon] db01-golden: FAILED — no output from the remote plan"]
            out, failed = _reconcile_zero_step_machines(machines, cfg, failed)
        self.assertEqual(out[0]["steps"][0]["name"], "nginx")  # untouched
        self.assertEqual(out[1]["steps"][0]["name"], "(empty plan)")
        self.assertEqual(failed, [])

    def test_unreachable_machine_with_configs_stays_failed(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _cfg(d, [{"name": "db01-golden", "configurations": ["nginx"]}])
            machines = [{"name": "db01-golden", "steps": [],
                         "error": "no output from the remote plan (exit 0)",
                         "exit_status": 0}]
            failed = ["[nakon] db01-golden: FAILED — no output from the remote plan"]
            out, failed = _reconcile_zero_step_machines(machines, cfg, failed)
        # box HAS configurations: the silence is a genuine problem, not a no-op
        self.assertEqual(out[0]["error"], "no output from the remote plan (exit 0)")
        self.assertEqual(len(failed), 1)

    def test_unreadable_config_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.json"
            p.write_text("{not json")
            machines = [{"name": "x", "steps": [], "error": ZERO_STEP_ERR}]
            out, failed = _reconcile_zero_step_machines(machines, p, ["failed"])
        self.assertEqual(out, machines)
        self.assertEqual(failed, ["failed"])


if __name__ == "__main__":
    unittest.main()
