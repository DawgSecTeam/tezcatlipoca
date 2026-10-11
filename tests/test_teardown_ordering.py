"""The rehearsal report grades red's OWN world.json, so the final red pull must land
before the report is generated.

Live run4 2026-10-09: evidence/red/world.json held 2 Windows footholds (dc01 + win02,
Administrator, escalation system), but INTERACTION.md — written two seconds EARLIER —
read windows_footholds 0 and failed the rehearsal gate. The final pull lived only inside
teardown_red, which stage_teardown calls AFTER write_interaction_report, so the report
always raced it and graded red's previous state.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from scrim import red_link, teardown_stage, test_folder


def _fake_proc(*_a, **_k):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class TeardownOrdering(unittest.TestCase):
    def setUp(self):
        self.order = []
        # stage_teardown's tail asserts red01 is really gone; these cases are about order.
        self.addCleanup(mock.patch.object(teardown_stage, "_red_vm_still_exists",
                                          lambda *a, **k: False).start)

    def _args(self):
        return SimpleNamespace(competition="scrim-one", teams=1, duration_min=75,
                               keep_range=False, red_ip="10.0.0.198", red_vmid=999,
                               run_dir="/tmp/does-not-matter", test_dir=None,
                               red_tunnel=None)

    def test_red_pull_precedes_the_report(self):
        order, args = self.order, self._args()
        with mock.patch.object(red_link, "pull_red_state",
                               lambda *a, **k: order.append("pull")), \
             mock.patch.object(test_folder, "collect_run_artifacts",
                               lambda *a, **k: order.append("collect")), \
             mock.patch.object(test_folder, "write_interaction_report",
                               lambda *a, **k: order.append("report")), \
             mock.patch.object(teardown_stage, "teardown_red",
                               lambda *a, **k: order.append("teardown_red")), \
             mock.patch.object(teardown_stage.procs, "run", _fake_proc):
            teardown_stage.stage_teardown(args, creds={"user": "sysadmin"})
        self.assertEqual(order[:3], ["pull", "collect", "report"],
                         f"the final red pull must precede the report, got {order}")

    def test_without_creds_nothing_is_collected(self):
        order, args = self.order, self._args()
        with mock.patch.object(red_link, "pull_red_state",
                               lambda *a, **k: order.append("pull")), \
             mock.patch.object(test_folder, "collect_run_artifacts",
                               lambda *a, **k: order.append("collect")), \
             mock.patch.object(teardown_stage, "teardown_red",
                               lambda *a, **k: order.append("teardown_red")), \
             mock.patch.object(teardown_stage.procs, "run", _fake_proc):
            teardown_stage.stage_teardown(args, creds=None)
        self.assertEqual([x for x in order if x in ("pull", "collect")], [],
                         f"a teardown with no creds must not collect (got {order})")


if __name__ == "__main__":
    unittest.main()
