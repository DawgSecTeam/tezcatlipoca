"""The deploy() sequencer after the phase split: order, checkpoint gating, banners, resume."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
import deploy  # noqa: E402
from _deploy_patch import dpatch  # noqa: E402
from infra import gates as dl_gates  # noqa: E402


SKIP_BANNERS = [
    "[1/8] Skipped (resume) — keeping the run's markers in place.",
    "[2/8] Skipped (resume) — not re-creating the engine.",
    "[3/8] Skipped (resume).",
    "[4/8] Skipped (resume).",
    "[5/8] Skipped (resume).",
    "[6/8] Skipped (resume).",
    "[7/8] Skipped (resume).",
    "[8/8] Skipped (resume).",
]

def _ctx(comp_dir, from_phase, state=None):
    return deploy.DeployContext(
        comp_dir=comp_dir, comp_name=comp_dir.name,
        state_path=comp_dir / "estate-state.json", from_phase=from_phase,
        resuming=from_phase > 1, assume_yes=True, name="probe", scenario="probe",
        box_username="ubuntu", credlist_usernames=["admin"], nakon_jobs=4,
        apt_cache=True, injects=[], packet_pw=None,
        state=state if state is not None else {"last_phase": 3},
        teams={}, number_of_teams=0, admin_password="a", scoring_password="s",
        postgres_password="p",
        redis_password="r", box_password="b", box_creds={}, domain_creds=None,
        inject_password=None, placement=None, node="pve", engine_vmid=1000,
        engine_mgmt_ip="10.0.0.1", boxes=[], boxes_by_name={}, unbooted=set(),
        nakon_config_path=comp_dir / "nakon.json",
        golden_config_path=comp_dir / "golden.json",
        predomain_config_path=comp_dir / "predomain.json",
        final_config_path=comp_dir / "final.json",
        golden_inputs={}, golden_hashes={},
 ssh_key_abs="/k",
        all_targets=[], managed_targets=[], linux_targets=[], windows_targets=[],
    )


def _stub_phases(calls):
    def make(n):
        def phase(ctx):
            calls.append(("phase", n))
        return phase
    return tuple(make(n) for n in range(1, 9))


class BannerSet(unittest.TestCase):
    # The [N/8] banners operators and incident logs key on. Byte-identical, once each.

    def _run_resume_from_9(self, comp_dir):
        ctx = _ctx(comp_dir, 9)
        buf = io.StringIO()
        with dpatch("prepare", return_value=ctx), \
                dpatch("connect_estate"), \
                dpatch("finish_deploy"):
            with contextlib.redirect_stdout(buf):
                deploy.deploy(comp_dir, assume_yes=True, from_phase=9)
        return buf.getvalue()

    def test_resume_emits_every_skip_banner_exactly_once_in_phase_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run_resume_from_9(Path(d))
        emitted = [ln.rstrip("\n") for ln in out.splitlines() if "Skipped (resume)" in ln]
        self.assertEqual(emitted, SKIP_BANNERS)

class Sequencer(unittest.TestCase):
    def _run(self, comp_dir, from_phase, phases, calls, hold_start=False):
        ctx = _ctx(comp_dir, from_phase)
        with dpatch("prepare", return_value=ctx), \
                dpatch("PHASES", phases), \
                dpatch("connect_estate",
                             side_effect=lambda c: calls.append(("connect", 2))), \
                dpatch("finish_deploy"), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.deploy(comp_dir, assume_yes=True, from_phase=from_phase,
                          hold_start=hold_start)
        return ctx
    def test_phases_run_in_order_and_connect_estate_sits_after_phase_2(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            ctx = self._run(Path(d), 1, _stub_phases(calls), calls)
        self.assertEqual(calls, [("phase", 1), ("phase", 2), ("connect", 2),
                                 ("phase", 3), ("phase", 4), ("phase", 5),
                                 ("phase", 6), ("phase", 7), ("phase", 8)])
        self.assertEqual(ctx.state["last_phase"], 8)


    def test_skipped_phases_do_not_checkpoint(self):
        # Checkpointing skipped phases would move the resume guard's answer
        # forward for phases this run never ran.
        def boom(ctx):
            raise RuntimeError("boom")
        calls = []
        phases = list(_stub_phases(calls))
        phases[5] = boom
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), 6)
            with dpatch("prepare", return_value=ctx), \
                    dpatch("PHASES", tuple(phases)), \
                    dpatch("connect_estate"), \
                    dpatch("finish_deploy"), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    deploy.deploy(Path(d), assume_yes=True, from_phase=6)
        self.assertEqual(ctx.state["last_phase"], 3)

    def test_hold_start_is_recorded_and_survives_a_resume(self):
    # Separate comp dirs: the deploy lock is held per dir for the process's life.
        calls = []
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            ctx = self._run(Path(d1), 1, _stub_phases(calls), calls, hold_start=True)
            self.assertTrue(ctx.state["hold_start"])
            # prepare reads hold_start back from the state file; here the dict is carried over.
            ctx2 = _ctx(Path(d2), 8, state=dict(ctx.state))
            with dpatch("prepare", return_value=ctx2), \
                    dpatch("PHASES", _stub_phases(calls)), \
                    dpatch("connect_estate"), \
                    dpatch("finish_deploy"), \
                    contextlib.redirect_stdout(io.StringIO()):
                deploy.deploy(Path(d2), assume_yes=True, from_phase=8)
        self.assertTrue(ctx2.state["hold_start"])


class CancelledPrepare(unittest.TestCase):
    def test_declining_the_prompt_runs_no_phase_and_keeps_the_lock(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            comp_dir = Path(d)
            with dpatch("prepare", return_value=None), \
                    dpatch("PHASES", _stub_phases(calls)), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = deploy.deploy(comp_dir, assume_yes=False)
            self.assertIsNone(result)
            self.assertEqual(calls, [])
            # The deploy lock is a process-lifetime flock; it stays held here.
            self.assertIn(str(comp_dir), dl_gates._DEPLOY_LOCKS)


if __name__ == "__main__":
    unittest.main()
