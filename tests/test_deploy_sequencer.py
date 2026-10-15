"""The deploy() sequencer after the phase split: phase order, checkpoint gating,
the banner set (exactly one per phase, byte-identical), the terraform-context hook
between phases 2 and 3, and the mid-phase failure/resume hint.

All offline: prepare() and the phase functions are stubbed, or the REAL phases are
driven down their resume path (from_phase=9), which prints their banners and
touches no infrastructure. The from_phase=9 run is what makes the banner set a
regression test — the phase-extraction commits briefly emitted the phase 4-7 skip
banners twice, and nothing caught it.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import deploy  # noqa: E402
from _deploy_patch import dpatch  # noqa: E402
from infra import gates as dl_gates  # noqa: E402


SKIP_BANNERS = [
    "[1/8] Skipped (resume) — leaving existing VMs/bridges in place.",
    "[2/8] Skipped (resume) — not re-running terraform apply.",
    "[3/8] Skipped (resume).",
    "[4/8] Skipped (resume).",
    "[5/8] Skipped (resume).",
    "[6/8] Skipped (resume).",
    "[7/8] Skipped (resume).",
    "[8/8] Skipped (resume).",
]

def _ctx(comp_dir, from_phase, state=None):
# A DeployContext carrying only what the sequencer and finish path touch.
    return deploy.DeployContext(
        comp_dir=comp_dir, comp_name=comp_dir.name,
        state_path=comp_dir / ".deploy_state.json", from_phase=from_phase,
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
        golden_inputs={}, golden_hashes={}, frozen_keep=set(), tf_dir=comp_dir,
        tfvars_path=comp_dir / "terraform.tfvars.json", tfvars={}, ssh_key_abs="/k",
        all_targets=[], managed_targets=[], linux_targets=[], windows_targets=[],
    )


def _stub_phases(calls):
# Eight stub phases recording their number; PHASES order is what is under test.
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
                dpatch("connect_terraform"), \
                dpatch("finish_deploy"):
            with contextlib.redirect_stdout(buf):
                deploy.deploy(comp_dir, assume_yes=True, from_phase=9)
        return buf.getvalue()

    def test_resume_emits_every_skip_banner_exactly_once_in_phase_order(self):
        with tempfile.TemporaryDirectory() as d:
            # Real phases: each prints its own skip banner and returns.
            out = self._run_resume_from_9(Path(d))
        emitted = [ln.rstrip("\n") for ln in out.splitlines() if "Skipped (resume)" in ln]
        self.assertEqual(emitted, SKIP_BANNERS)

class Sequencer(unittest.TestCase):
# deploy() walks PHASES, gates checkpointing, and hooks the terraform read.

    def _run(self, comp_dir, from_phase, phases, calls):
        ctx = _ctx(comp_dir, from_phase)
        with dpatch("prepare", return_value=ctx), \
                dpatch("PHASES", phases), \
                dpatch("connect_terraform",
                             side_effect=lambda c: calls.append(("connect", 2))), \
                dpatch("finish_deploy"), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy.deploy(comp_dir, assume_yes=True, from_phase=from_phase)
        return ctx
    def test_phases_run_in_order_and_connect_terraform_sits_after_phase_2(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            ctx = self._run(Path(d), 1, _stub_phases(calls), calls)
        self.assertEqual(calls, [("phase", 1), ("phase", 2), ("connect", 2),
                                 ("phase", 3), ("phase", 4), ("phase", 5),
                                 ("phase", 6), ("phase", 7), ("phase", 8)])
        self.assertEqual(ctx.state["last_phase"], 8)

    def test_connect_terraform_is_not_gated_on_phase_2_running(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            self._run(Path(d), 3, _stub_phases(calls), calls)
        self.assertEqual([c for c in calls if c[0] == "connect"], [("connect", 2)])

    def test_skipped_phases_do_not_checkpoint(self):
        # from_phase=6 over a state file whose last completed phase was 3, and phase
        # 6 dies: last_phase must stay 3. Checkpointing the skipped phases would
        # move the resume guard's answer forward to 5 for phases this run never ran.
        def boom(ctx):
            raise RuntimeError("boom")
        calls = []
        phases = list(_stub_phases(calls))
        phases[5] = boom
        with tempfile.TemporaryDirectory() as d:
            ctx = _ctx(Path(d), 6)
            with dpatch("prepare", return_value=ctx), \
                    dpatch("PHASES", tuple(phases)), \
                    dpatch("connect_terraform"), \
                    dpatch("finish_deploy"), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    deploy.deploy(Path(d), assume_yes=True, from_phase=6)
        self.assertEqual(ctx.state["last_phase"], 3)


class FailureHandling(unittest.TestCase):
# The failure banner + resume hint, with current_phase set by the sequencer.

    def _fail(self, comp_dir, from_phase, exc, phases):
        ctx = _ctx(comp_dir, from_phase)
        buf = io.StringIO()
        with dpatch("prepare", return_value=ctx), \
                dpatch("PHASES", phases), \
                dpatch("connect_terraform"), \
                dpatch("finish_deploy"):
            with contextlib.redirect_stdout(buf):
                with self.assertRaises(type(exc)) as raised:
                    deploy.deploy(comp_dir, assume_yes=True, from_phase=from_phase)
        return ctx, buf.getvalue(), raised.exception

    def test_mid_phase_failure_names_the_phase_and_the_resume_target(self):
        def boom(ctx):
            raise RuntimeError("boom")
        phases = list(_stub_phases([]))
        phases[2] = boom  # phase 3
        with tempfile.TemporaryDirectory() as d:
            ctx, out, exc = self._fail(Path(d), 1, RuntimeError("boom"), tuple(phases))
        self.assertIn("Deploy failed during phase 3 of '", out)
        self.assertIn("--from-phase 3 --yes", out)
        self.assertEqual(str(exc), "boom")
        # phases 1-2 completed and checkpointed, phase 3 did not
        self.assertEqual(ctx.state["last_phase"], 2)

    def test_already_exists_mismatch_suggests_a_full_re_run_from_phase_1(self):
        def boom(ctx):
            raise RuntimeError("VM 101 already exists")
        phases = list(_stub_phases([]))
        phases[3] = boom  # phase 4
        with tempfile.TemporaryDirectory() as d:
            _ctx_, out, _ = self._fail(Path(d), 4, RuntimeError("x"), tuple(phases))
        self.assertIn("Deploy failed during phase 4 of '", out)
        self.assertIn("--from-phase 1 --yes", out)
        self.assertIn("state mismatch", out)


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
            # Same as the old inline early return: the lock stays held (it is the
            # process-lifetime flock), so a second deploy in this process refuses.
            self.assertIn(str(comp_dir), dl_gates._DEPLOY_LOCKS)


if __name__ == "__main__":
    unittest.main()
