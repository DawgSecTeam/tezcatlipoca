"""The single-gate-point contract: every pre-flight refusal lives in infra/gates.py and
runs inside prepare() BEFORE a credential is minted, a competition input written, or a target
enumerated. Ordering is the property -- a refusal after the mint leaves a credential-desync
corpse caught live."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from infra import gates as dl_gates  # noqa: E402
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

        def set_prior(ctx, *a, **k):
            ctx.state_path = Path("/nonexistent/estate-state.json")
            ctx.previous_state = {}
            ctx.resuming = from_phase > 1

        patches = {
            "resolve_engine_vmid": step("engine_vmid"),
            "load_competition_spec": step("spec"),
            "load_prior_deploy_state": step("prior_state", set_prior),
            "check_resume_gates": step("GATE:resume"),
            "load_competition_inputs": step("inputs"),
            "resolve_competition_teams": step("teams"),
            "apply_engine_placement": step("placement"),
            "engine_mgmt_ip_from_env": lambda *a, **k: calls.append("mgmt_ip") or "10.0.0.2",
            "run_range_gates": step("GATE:range"),
            "mint_competition_secrets": step("MINT"),
            "generate_stage_configs_and_hashes": step("configs"),
            "build_estate_inputs": step("estate-inputs"),
            "enumerate_deploy_targets": step("targets"),
        }
        started = [patch.object(dl_prepare, n, side_effect=f) for n, f in patches.items()]
        for p in started:
            p.start()
            self.addCleanup(p.stop)
        result = dl_prepare.prepare(Path("/nonexistent"), assume_yes=assume_yes,
                                    from_phase=from_phase)
        return result, calls

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
