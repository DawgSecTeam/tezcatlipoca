"""Teardown refuses a deployed comp whose per-comp terraform state is missing."""

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from destroy_gate_ops import require_terraform_state  # noqa: E402


class RequireTerraformStateTests(unittest.TestCase):
    def test_deployed_comp_without_state_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as cm:
                require_terraform_state(Path(d), {"last_phase": 7, "run_id": "run-x"})
            self.assertIn("Reconstruct the state file", str(cm.exception))

    def test_state_present_passes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "terraform").mkdir()
            (Path(d) / "terraform" / "terraform.tfstate").write_text("{}")
            require_terraform_state(Path(d), {"last_phase": 7, "run_id": "run-x"})

    def test_never_applied_comp_passes(self):
        with tempfile.TemporaryDirectory() as d:
            require_terraform_state(Path(d), {"last_phase": 1, "run_id": "run-x"})


if __name__ == "__main__":
    unittest.main()
