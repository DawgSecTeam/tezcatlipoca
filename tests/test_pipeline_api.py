"""create-competition.py --plan-only runs end to end (smoke for the thin CLI)."""

import subprocess
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


class PipelineApiSurfaceTests(unittest.TestCase):
    def test_create_competition_plan_only_still_runs(self):
        if not (_REPO / "competitions" / "example" / "boxes.json").exists():
            self.skipTest("competitions/example missing")
        out = subprocess.run(
            [sys.executable, "create-competition.py", "--competition", "example", "--plan-only"],
            capture_output=True, text=True, cwd=_REPO, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PLAN for 'example'", out.stdout)


if __name__ == "__main__":
    unittest.main()
