"""`terraform init` is serialised per shared provider cache.

The cache (TF_PLUGIN_CACHE_DIR, `<home>/.cache/tezcatlipoca/terraform-plugins`) is shared by
every run on a host, and the dashboard deliberately allows concurrent runs. Two inits at once
meant one process installed `terraform-provider-null` while the other had just exec'd it, and
the loser died with `open .../terraform-provider-null_v3.3.2_x5: text file busy` (live:
run-1d713484, 2026-10-14, while a second competition was deploying on the same host).

This test drives the real path — two `run_terraform(["init"], …)` calls in two processes against
a stub `terraform` — and asserts they do not overlap.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

STUB = "#!/bin/sh\necho start $(date +%s.%N)\nsleep 1\n"

CHILD = """
import sys
sys.path.insert(0, {repo!r})
from common.utils import run_terraform
run_terraform(["init"], cwd={cwd!r}, env={env!r})
"""


class TerraformInitLock(unittest.TestCase):

    def test_two_concurrent_inits_do_not_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bindir = tmp / "bin"
            bindir.mkdir()
            stub = bindir / "terraform"
            stub.write_text(STUB)
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
            cache = tmp / "plugin-cache"
            env = {"PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                   "TF_PLUGIN_CACHE_DIR": str(cache)}
            code = CHILD.format(repo=str(_REPO), cwd=str(tmp), env=env)
            procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                      text=True) for _ in range(2)]
            starts = []
            for proc in procs:
                out, _ = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, out)
                starts.append(float(out.split("start")[1].strip()))
            self.assertGreaterEqual(max(starts) - min(starts), 0.8,
                                    f"inits overlapped (starts: {sorted(starts)})")
            # the lock lives with the cache it protects
            self.assertTrue((cache / ".init.lock").exists())

    def test_a_non_init_command_takes_no_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            binpath = tmp / "bin"
            binpath.mkdir()
            (binpath / "terraform").write_text("#!/bin/sh\nexit 0\n")
            (binpath / "terraform").chmod(0o755)
            env = {"PATH": f"{binpath}:{os.environ.get('PATH', '')}",
                   "TF_PLUGIN_CACHE_DIR": str(tmp / "cache")}
            from common.utils import run_terraform
            self.assertEqual(run_terraform(["plan"], cwd=tmp, env=env).returncode, 0)
            self.assertFalse((tmp / "cache" / ".init.lock").exists())


if __name__ == "__main__":
    unittest.main()
