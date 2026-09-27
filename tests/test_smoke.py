"""CI smoke gate: the class of bug that shipped in 108184c (NameErrors / unimported names on the
first executed line of run_nakon and the engine-template build). Fails on undefined names and
syntax errors only — stylistic pyflakes warnings (unused imports, bare f-strings) are ignored,
since create-competition.py deliberately re-exports everything. Also runs --plan-only offline."""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_FATAL = ("undefined name", "may be undefined", "syntax error", "invalid syntax",
          "unable to detect undefined names", "redefinition of unused")


class SmokeGate(unittest.TestCase):
    def test_pyflakes_no_undefined_names(self):
        if importlib.util.find_spec("pyflakes") is None:
            self.skipTest("pyflakes not installed")
        files = sorted(str(p) for p in _REPO.glob("*.py"))
        out = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                             capture_output=True, text=True, cwd=_REPO)
        bad = [l for l in (out.stdout + out.stderr).splitlines()
               if any(k in l.lower() for k in _FATAL)]
        self.assertEqual(bad, [], "pyflakes found fatal issues:\n" + "\n".join(bad))

    def test_plan_only_example(self):
        if not (_REPO / "competitions" / "example" / "boxes.json").exists():
            self.skipTest("competitions/example missing")
        out = subprocess.run(
            [sys.executable, "create-competition.py", "--competition", "example", "--plan-only"],
            capture_output=True, text=True, cwd=_REPO, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PLAN for 'example'", out.stdout)


if __name__ == "__main__":
    unittest.main()
