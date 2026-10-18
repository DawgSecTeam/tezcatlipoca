"""The timing sidecar: the AD work is filed under its own phase, and a row that ran inside
another is detail rather than extra wall clock.

Live round 2: `domain_adds` was written as phase 6 while its parent `domains` is phase 7, and
the aggregators summed parent and children, so phase 7 reached ~1.8x the run's wall clock
(children 4994.7 s under a 3062.1 s parent; phase 6's own work was 168.9 s).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from artifacts_lib import report as a_report  # noqa: E402
from common import timing  # noqa: E402
from guests import domain_ops  # noqa: E402

_STAMP = "%Y-%m-%dT%H:%M:%S"
BASE = 1_700_000_000


def _line(path, phase, op, seconds, end):
    with open(path, "a") as f:
        f.write(json.dumps({"ts": time.strftime(_STAMP, time.localtime(end)),
                            "phase": str(phase), "op": op, "target": "",
                            "seconds": seconds}) + "\n")


class DomainPhaseLabel(unittest.TestCase):
# The domain chain runs inside phase 7's `domains` span (deploy_lib/phases/final.py).

    def test_domain_adds_is_filed_under_phase_7(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp_dir = Path(tmp)
            (comp_dir / "domain_roles.json").write_text(json.dumps({"dc01": "dc"}))
            config = comp_dir / "nakon.json"
            config.write_text(json.dumps({"machines": [
                {"name": "dc01-team105", "ip": "192.168.105.6", "configurations": []}]}))
            boxes = [{"name": "dc01", "template": "base-windows-server"}]
            with patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "node"}), \
                    patch.object(domain_ops, "guest_agent_exec_windows",
                                 return_value=(0, "0|WORKGROUP", "")), \
                    patch.object(domain_ops, "_run_single_nakon_config"), \
                    patch.object(domain_ops, "wait_for_guest_agent", return_value=True), \
                    patch.object(domain_ops, "wait_for_windows_sshd"), \
                    patch.object(domain_ops, "wait_for_adws", return_value="S-1-5-21-1"):
                domain_ops.deploy_domain_configs(
                    {"team1": {"identifier": "105"}}, boxes, comp_dir,
                    config, "key", "scoring", "10.0.0.248", "password")
            rows = timing.read_timings(comp_dir / timing.TIMINGS_NAME)
        adds = [r for r in rows if r["op"] == "domain_adds"]
        self.assertTrue(adds, "the domain_adds span was timed")
        self.assertEqual({r["phase"] for r in adds}, {"7"})


class NoDoubleCount(unittest.TestCase):
# A nested span's seconds are already inside its parent's.

    def _sidecar(self, root):
        path = root / timing.TIMINGS_NAME
        _line(path, 7, "domains", 3062.1, BASE + 3062)
        _line(path, 7, "domain_adds", 900.0, BASE + 1000)
        # Pre-fix files carry the same nesting with the old phase label; containment is
        # what identifies the span, so those files are read correctly too.
        _line(path, 6, "domain_adds", 1500.0, BASE + 2000)
        _line(path, 4, "golden_build", 300.0, BASE + 4000)
        return path



    def test_report_phase_totals_do_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "evidence" / "harness"
            harness.mkdir(parents=True)
            self._sidecar(harness)
            totals, slowest = a_report.timing_rows(tmp)
        self.assertEqual(totals, [("7", 3062.1), ("4", 300.0)])
        self.assertEqual(slowest[0][1], "7")


if __name__ == "__main__":
    unittest.main()
