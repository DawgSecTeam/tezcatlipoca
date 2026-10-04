"""The deploy() sequencer after the phase split: phase order, checkpoint gating,
the banner set (exactly one per phase, byte-identical), the terraform-context hook
between phases 2 and 3, and the mid-phase failure/resume hint.

All offline: prepare() and the phase functions are stubbed, or the REAL phases are
driven down their resume path (from_phase=9), which prints their banners and
touches no infrastructure. The from_phase=9 run is what makes the banner set a
regression test — the phase-extraction commits briefly emitted the phase 4-7 skip
banners twice, and nothing caught it.
"""

import ast
import contextlib
import inspect
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import deploy  # noqa: E402
from deploy_lib import phases  # noqa: E402
from _deploy_patch import dpatch  # noqa: E402
from deploy_lib import gates as dl_gates  # noqa: E402


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

# Every "[N/8]"-tagged print each phase can emit, in the order the phase emits it:
# its resume guard first, then the run banners. Alternative run banners (phase 5
# with/without firewalls, phase 6 marker-vs-sweep) are all listed in order.
RUN_BANNERS = {
    "phase1_cleanup": [
        SKIP_BANNERS[0],
        "[1/8] Cleaning up previous deployment (parallel; deletes are metadata-light "
        "— the datastore-saturation hazard belongs to bulk clone writes, not deletes)...",
    ],
    "phase2_engine_template": [
        SKIP_BANNERS[1],
        "[2/8] Terraform apply #1 (engine from template + bridges; team boxes "
        "come in apply #2)...",
    ],
    "phase3_prepare_engine": [
        SKIP_BANNERS[2],
        "[3/8] Preparing scoring engine from template (fresh volumes, event.conf)...",
    ],
    "phase4_golden_set": [
        SKIP_BANNERS[3],
        "[4/8] Building the golden set (plant once per box type, convert to template)...",
        "[4b/8] Terraform apply #2: every team as a linked clone of the golden set...",
    ],
    "phase5_firewall_bootstrap": [
        SKIP_BANNERS[4],
        "[5/8] No in-path firewall in this lineup — skipping.",
        "[5/8] Bootstrapping the in-path firewalls (console → fetch → reboot → "
        "engine cutover)...",
    ],
    "phase6_repair_sweep": [
        SKIP_BANNERS[5],
        "[6/8] Resume marker present — post-clone sweep already done; skipping",
        "[6/8] Repair-stage sweep (sshd/sudoers) on every team box...",
    ],
    "phase7_domains_and_final": [
        SKIP_BANNERS[6],
        "  Configuring Windows AD domains (if any)...",
    ],
    "phase8_seed": [
        SKIP_BANNERS[7],
        "[8/8] Seeding competition and creating injects...",
    ],
}


def _no_artifact_gates():
    """Neutralize the checkpoint's existence gate for sequencing tests.

    `checkpoint(3|4)` now verifies the phase's output exists before recording it (C1),
    which needs a live Proxmox. These tests exercise ORDER and checkpoint gating, not
    artifact verification, so the gate is stubbed here and tested for real in
    tests/test_resume_existence.py."""
    return patch.object(deploy.DeployContext, "_verify_phase_artifacts",
                        lambda self, n: None)


def _ctx(comp_dir, from_phase, state=None):
    """A DeployContext carrying only what the sequencer and finish path touch."""
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
        repair_config_path=comp_dir / "repair.json",
        final_config_path=comp_dir / "final.json",
        golden_inputs={}, golden_hashes={}, frozen_keep=set(), tf_dir=comp_dir,
        tfvars_path=comp_dir / "terraform.tfvars.json", tfvars={}, ssh_key_abs="/k",
        all_targets=[], managed_targets=[], linux_targets=[], windows_targets=[],
    )


def _stub_phases(calls):
    """Eight stub phases recording their number; PHASES order is what is under test."""
    def make(n):
        def phase(ctx):
            calls.append(("phase", n))
        return phase
    return tuple(make(n) for n in range(1, 9))


class BannerSet(unittest.TestCase):
    """The [N/8] banners operators and incident logs key on. Byte-identical, once each."""

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

    def test_run_banners_are_present_in_the_source_in_phase_order(self):
        # The run banners only print on a real (infra-touching) run, so assert the
        # source: each phase function's banner literals, in function order, must
        # equal the captured list byte for byte, and the phases must sit in PHASES
        # order in the module.
        funcs = _phase_functions()
        names = [p.__name__ for p in phases.PHASES]
        self.assertEqual(names, list(RUN_BANNERS))
        for name in names:
            literals = _print_literals(funcs[name])
            expected = RUN_BANNERS[name]
            got = [lit for lit in literals if lit in expected]
            self.assertEqual(got, expected, name)

    def test_each_phase_skips_on_its_own_phase_number(self):
        # The guard must compare against the phase's [N/8] number, not a neighbour's.
        funcs = _phase_functions()
        for index, phase in enumerate(phases.PHASES, 1):
            fn = funcs[phase.__name__]
            compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                        and isinstance(n.left, ast.Attribute) and n.left.attr == "from_phase"]
            self.assertTrue(compares, phase.__name__)
            self.assertEqual(compares[0].comparators[0].value, index, phase.__name__)


def _phase_functions():
    """{phase name: its ast FunctionDef}, parsed from each phase function's own source."""
    out = {}
    for phase in phases.PHASES:
        tree = ast.parse(textwrap.dedent(inspect.getsource(phase)))
        out[phase.__name__] = tree.body[0]
    return out


def _print_literals(fn):
    """Every string literal printed by fn, in SOURCE order (ast.walk is not ordered)."""
    found = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
                and node.args):
            try:
                value = ast.literal_eval(node.args[0])
            except Exception:
                continue
            if isinstance(value, str):
                found.append((node.lineno, value))
    return [value for _lineno, value in sorted(found)]


class Sequencer(unittest.TestCase):
    """deploy() walks PHASES, gates checkpointing, and hooks the terraform read."""

    def _run(self, comp_dir, from_phase, phases, calls):
        ctx = _ctx(comp_dir, from_phase)
        with dpatch("prepare", return_value=ctx), \
                dpatch("PHASES", phases), \
                dpatch("connect_terraform",
                             side_effect=lambda c: calls.append(("connect", 2))), \
                dpatch("finish_deploy"), \
                _no_artifact_gates(), \
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
    """The failure banner + resume hint, with current_phase set by the sequencer."""

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
