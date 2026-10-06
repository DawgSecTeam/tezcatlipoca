"""The single-gate-point contract: every pre-flight refusal lives in deploy_lib/gates.py and
runs inside prepare() BEFORE a credential is minted, a tfvars file written, or a target
enumerated. Ordering is the property -- a refusal after the mint leaves a credential-desync
corpse (2026-09-30 testcomp-7box)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from deploy_lib import gates as dl_gates  # noqa: E402
from deploy_lib import prepare as dl_prepare  # noqa: E402
from deploy_lib import runner as dl_runner  # noqa: E402


class PrepareGateOrder(unittest.TestCase):
    def _run_prepare(self, from_phase=1, assume_yes=True):
        calls = []

        def step(name, effect=None):
            def _fn(*a, **k):
                calls.append(name)
                if effect:
                    effect(*a, **k)
            return _fn

        def set_prior(prior, *a, **k):
            prior.state_path = Path("/nonexistent/.deploy_state.json")
            prior.previous_state = {}
            prior.resuming = from_phase > 1

        patches = {
            "resolve_engine_vmid": step("engine_vmid"),
            "load_competition_spec": step("spec"),
            "load_prior_deploy_state": step("prior_state", set_prior),
            "check_resume_gates": step("GATE:resume"),
            "load_competition_inputs": step("inputs"),
            "resolve_competition_teams": step("teams"),
            "apply_engine_placement": step("placement"),
            "engine_mgmt_ip_from_env": lambda: calls.append("mgmt_ip") or "10.0.0.2",
            "run_range_gates": step("GATE:range"),
            "mint_competition_secrets": step("MINT"),
            "generate_stage_configs_and_hashes": step("configs"),
            "build_terraform_inputs": step("tfvars"),
            "enumerate_deploy_targets": step("targets"),
            "assemble_deploy_context": lambda *a, **k: calls.append("assemble") or "CTX",
        }
        started = [patch.object(dl_prepare, n, side_effect=f) for n, f in patches.items()]
        for p in started:
            p.start()
            self.addCleanup(p.stop)
        result = dl_prepare.prepare(Path("/nonexistent"), assume_yes=assume_yes,
                                    from_phase=from_phase)
        return result, calls

    def test_both_gates_run_before_the_mint_and_the_tfvars_write(self):
        result, calls = self._run_prepare()
        self.assertEqual(result, "CTX")
        gates = [c for c in calls if c.startswith("GATE")]
        self.assertEqual(gates, ["GATE:resume", "GATE:range"])
        for gate in gates:
            self.assertLess(calls.index(gate), calls.index("MINT"))
            self.assertLess(calls.index(gate), calls.index("tfvars"))
            self.assertLess(calls.index(gate), calls.index("targets"))

    def test_the_resume_gate_precedes_placement_and_the_engine_lock(self):
        _result, calls = self._run_prepare()
        self.assertLess(calls.index("GATE:resume"), calls.index("placement"))

    def test_a_resume_never_mints(self):
        _result, calls = self._run_prepare(from_phase=4)
        self.assertNotIn("MINT", calls)
        self.assertIn("GATE:range", calls)


class DeployLock(unittest.TestCase):
    def test_deploy_takes_the_lock_before_preparing(self):
        order = []
        with patch.object(dl_runner, "acquire_deploy_lock",
                          side_effect=lambda d: order.append("lock")), \
                patch.object(dl_runner, "prepare",
                             side_effect=lambda *a, **k: order.append("prepare")):
            dl_runner.deploy(Path("/nonexistent"))
        self.assertEqual(order, ["lock", "prepare"])

    def test_a_held_lock_refuses(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            comp = Path(d)
            first = comp / "a"
            first.mkdir()
            dl_gates.acquire_deploy_lock(first)
            # Same dir, second open file description in this process: flock conflicts.
            with self.assertRaises(SystemExit) as raised:
                dl_gates.acquire_deploy_lock(first)
        self.assertIn("already holds the lock", str(raised.exception))
        dl_gates._DEPLOY_LOCKS.clear()


if __name__ == "__main__":
    unittest.main()
