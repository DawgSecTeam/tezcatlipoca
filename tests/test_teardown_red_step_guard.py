"""The standalone teardown's red01 step must never reach another session's red box.

caught live (run-44a106d2): destroying a workspace whose run had no red agent ran
`badauto destroy` against the HOST's default config (built-in red_vmid 999), found another
session's tdred-2026 red01 there, and — because the reclaim call passed no comp_name —
destroyed it despite its marker naming a different competition. Two guards: skip the step
when the run staged no red (mirrors the scrim harness's c8a2076), and always arm the
reclaim's another-competition guard with this comp's name.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from teardown.steps import RedStep, TeardownContext  # noqa: E402


def _ctx(comp_dir, deployed_state=None):
    return TeardownContext(comp_dir=Path(comp_dir), env={}, teams={}, boxes=[],
                           deployed_state=deployed_state or {}, ownership=None,
                           placement=None, nodes=["n"], default_node="n", inventory=None)


class RedStepSkip(unittest.TestCase):
    def test_no_red_trace_skips_badauto_entirely(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(d)   # no .badauto-config.yaml, no assume_breach record
            with patch("red.red_plant_ops.destroy_red") as destroy:
                result = RedStep().run(ctx)
        destroy.assert_not_called()
        self.assertEqual(result.status, "skipped")

    def test_a_recorded_red_vmid_still_runs_the_destroy(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(d, {"assume_breach": {"vmid": 3570}})
            with patch("red.red_plant_ops.destroy_red",
                       side_effect=lambda *a, **k: SimpleNamespace(returncode=0,
                                                                   stdout="", stderr="")):
                result = RedStep().run(ctx)
        destroy_status = result.status
        self.assertEqual(destroy_status, "ok")


class RedStepReclaimGuard(unittest.TestCase):
    def test_reclaim_is_armed_with_this_comps_name(self):
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            (comp / ".badauto-config.yaml").write_text("competition: comp\ndeploy:\n  red_vmid: 3570\n")
            ctx = _ctx(comp)
            target = SimpleNamespace(returncode=17, stdout="", stderr="")
            calls = {}
            with patch("red.red_plant_ops.destroy_red", return_value=target), \
                    patch("red.red_plant_ops.parse_teardown_refusal",
                          return_value={"saw": "comp=tdred-2026"}), \
                    patch("red.red_plant_ops._red_target_vmid" if False else
                          "teardown.steps._red_target_vmid", return_value=999), \
                    patch("infra.red01.reclaim_unstamped_red",
                          side_effect=lambda vmid, comp_name=None, log=None:
                          (calls.setdefault("args", (vmid, comp_name)), False)[1]):
                result = RedStep().run(ctx)
        self.assertEqual(calls["args"], (999, comp.name))
        self.assertEqual(result.status, "failed")   # foreign box: refused, not reclaimed

    def test_foreign_marked_red01_is_never_destroyed(self):
        # end to end through the real guard: a marker naming another competition means
        # reclaim returns False and the step fails loudly — nothing is destroyed
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            (comp / ".badauto-config.yaml").write_text("competition: comp\ndeploy:\n  red_vmid: 3570\n")
            ctx = _ctx(comp)
            target = SimpleNamespace(returncode=17, stdout="", stderr="")
            with patch("red.red_plant_ops.destroy_red", return_value=target), \
                    patch("red.red_plant_ops.parse_teardown_refusal",
                          return_value={"saw": "comp=tdred-2026"}), \
                    patch("teardown.steps._red_target_vmid", return_value=999), \
                    patch("infra.red01.vm_at",
                          return_value={"vmid": 999, "name": "bad-auto-red01",
                                        "node": "proxmox"}), \
                    patch("infra.red01._marker_of", return_value="tdred-2026"), \
                    patch("infra.ownership.destroy_vm_if_exists") as destroy:
                result = RedStep().run(ctx)
        destroy.assert_not_called()
        self.assertEqual(result.status, "failed")


if __name__ == "__main__":
    unittest.main()


class VerifyCleanSkippedRed(unittest.TestCase):
    """verify_clean demands a red01 absence proof only when a red01 step actually ran.

    A skipped red01 (the run staged no red) used to leave the teardown NOT CLEAN forever
    ("red01's vmid could not be verified gone") because the journal entry existed without
    any vmid to verify — caught live: run-bbd30b90: every destroy of a red-less comp
    exited 1."""

    def _report(self, journal_red01):
        from teardown import TeardownContext, TeardownEngine, Inventory
        from teardown.modes import build_terminal_plan
        from infra.ownership import Ownership
        import teardown.engine as engine_mod
        ctx = TeardownContext(
            comp_dir=Path("."), env={}, teams={"team1": {"identifier": "101"}},
            boxes=[], deployed_state={"run_id": "run-x"},
            ownership=Ownership("c1", "run-x"), placement=None, nodes=["n"],
            default_node="n", inventory=Inventory(["n"], api=lambda *a, **k: {"data": []}),
            full=True)
        plan = build_terminal_plan()
        done = {"red01": journal_red01} if journal_red01 else {}
        eng = TeardownEngine(ctx)
        with patch.object(engine_mod.TeardownEngine, "_load_journal",
                          return_value={"run_id": "run-x", "done": done}), \
                patch("red.red_plant_ops.red_vmid_candidates", return_value=[]), \
                patch("infra.client.live_vmids", side_effect=AssertionError("no vmid to check")):
            return eng.verify_clean(plan, [])

    def test_a_skipped_red01_needs_no_absence_proof(self):
        report = self._report({"detail": "no per-comp bad-auto config",
                               "status": "skipped"})
        self.assertFalse(any("red01" in p for p in report.problems), report.problems)

    def test_a_run_red01_step_without_a_vmid_still_demands_the_proof(self):
        report = self._report({"detail": "no output"})
        self.assertTrue(any("could not be verified gone" in p for p in report.problems),
                        report.problems)

    def test_journal_detail_carries_the_status(self):
        from teardown.steps import RedStep
        detail = RedStep.journal_detail(RedStep(), SimpleNamespace(status="skipped",
                                                                   detail="x"))
        self.assertEqual(detail["status"], "skipped")
