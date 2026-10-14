"""create-competition.py --plan-only runs end to end (smoke for the thin CLI)."""

import subprocess
import sys
import unittest
from pathlib import Path

from common.comp_paths import comps_root

_REPO = Path(__file__).resolve().parents[1]


class PipelineApiSurfaceTests(unittest.TestCase):
    def test_create_competition_plan_only_still_runs(self):
        if not (comps_root() / "example" / "boxes.json").exists():
            self.skipTest("example comp missing (comps root)")
        out = subprocess.run(
            [sys.executable, "create-competition.py", "--competition", "example", "--plan-only"],
            capture_output=True, text=True, cwd=_REPO, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PLAN for 'example'", out.stdout)


if __name__ == "__main__":
    unittest.main()
