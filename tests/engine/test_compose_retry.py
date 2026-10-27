"""compose up can miss docker compose's fresh-Postgres health window on a loaded
node; the containers keep starting, so the engine bootstrap retries instead of
failing the deploy (live: run-3d569d1f)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
from engine import engine_bootstrap_ops as ebo
from engine import engine_cmd_ops


class Retry(unittest.TestCase):
    def test_a_health_race_is_retried(self):
        calls = []

        def fake(ctx, cmd, **kw):
            calls.append(cmd)
            if "compose up" in cmd and sum("compose up" in c for c in calls) < 3:
                raise RuntimeError("dependency failed to start")
            return SimpleNamespace(stdout="")

        ctx = SimpleNamespace()
        # The retry now lives in engine_cmd_ops.start_quotient: patch the OWNING module.
        with patch.object(engine_cmd_ops, "_run_engine_cmd", side_effect=fake), \
             patch.object(ebo, "_run_engine_cmd", side_effect=fake), \
             patch.object(ebo, "push_quotient_env"), \
             patch.object(ebo, "_patch_quotient_injects_cmd", return_value="x"), \
             patch.object(ebo, "_install_firewall_and_healthcheck"), \
             patch.object(ebo.time, "sleep"):
            ebo.bootstrap_scoring_engine(ctx, "pg", "redis")
        self.assertEqual(sum("compose up" in c for c in calls), 3)


if __name__ == "__main__":
    unittest.main()
