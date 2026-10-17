"""Per-run team identifiers and engine mgmt IP: what makes two ranges of one competition
coexist on ONE node.

The dashboard runs every driver against a single shared engine env, so before these flags a
second concurrent range could only reuse that env's team-identifier block and management IP —
each one value for the whole run host (caught live: two ranges of one competition shared one
team block / one engine mgmt IP, and the
catalog's engine_mgmt_ip is per NODE, not per run). Offline; nothing here touches a node.
"""

import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from deploy_lib import cli as dl_cli  # noqa: E402
from deploy_lib import prepare as dl_prepare  # noqa: E402
from deploy_lib import runner as dl_runner  # noqa: E402


class ExplicitRunInputs(unittest.TestCase):
    def test_cli_parses_both_flags(self):
        args = dl_cli.build_parser().parse_args(
            ["--competition", "c", "--team-identifiers", "130,131",
             "--engine-mgmt-ip", "10.0.0.5"])
        self.assertEqual(args.team_identifiers, "130,131")
        self.assertEqual(args.engine_mgmt_ip, "10.0.0.5")

    def test_runner_forwards_both_to_prepare(self):
        seen = {}
        with patch.object(dl_runner, "acquire_deploy_lock"), \
                patch.object(dl_runner, "prepare",
                             side_effect=lambda *a, **k: seen.update(k) or None):
            dl_runner.deploy("/ws", num_teams=1, team_identifiers="130",
                             engine_mgmt_ip="10.0.0.5")
        self.assertEqual(seen["team_identifiers"], "130")
        self.assertEqual(seen["engine_mgmt_ip"], "10.0.0.5")

    def test_prepare_forwards_both_into_their_resolvers(self):
        # A flag wired into the parser but dropped in prepare() is the silent failure this
        # guards: the run would deploy onto the shared block with no complaint.
        seen = {}

        def record(name):
            def _fn(*a, **k):
                seen[name] = (a, k)
            return _fn

        noop = ["resolve_engine_vmid", "load_competition_spec", "load_prior_deploy_state",
                "check_resume_gates", "load_competition_inputs", "apply_engine_placement",
                "run_range_gates", "mint_competition_secrets",
                "generate_stage_configs_and_hashes", "build_estate_inputs",
                "enumerate_deploy_targets"]
        with contextlib.ExitStack() as stack:
            for name in noop:
                stack.enter_context(patch.object(dl_prepare, name,
                                                 side_effect=lambda *a, **k: None))
            stack.enter_context(patch.object(dl_prepare, "resolve_competition_teams",
                                             side_effect=record("teams")))
            stack.enter_context(patch.object(dl_prepare, "engine_mgmt_ip_from_env",
                                             side_effect=record("mgmt")))
            dl_prepare.prepare("/ws", num_teams=1, assume_yes=True,
                               team_identifiers="130", engine_mgmt_ip="10.0.0.5")
        self.assertEqual(seen["teams"][0][3], "130")
        self.assertEqual(seen["mgmt"][0][0], "10.0.0.5")


if __name__ == "__main__":
    unittest.main()
