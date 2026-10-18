"""Every driver entry point refuses at startup without the ambient Proxmox identity.

A deploy survives a missing endpoint (nodes.json supplies it), so the gap stayed invisible
until teardown, whose verify_clean reads the ambient config: two teardowns printed
`[estate] ok — destroyed 0` and then exited non-zero on `config 'proxmox.endpoint' is not
set`. Each script is run for real with the ambient identity neutralized.

Deleting the TEZ_* variables is not enough to isolate a checkout that HAS a `.env`:
`verifier/context.py` loads `REPO_ROOT/.env` regardless of cwd (destroy/redeploy/deploy load a
cwd-relative one, which a cwd outside the repo does hide). The keys are therefore set to the
empty string, which `load_dotenv` will not override, so the identity is absent for every
driver in every tree.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]


def _run(script, *argv, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TEZ_")}
    # Empty, not absent: load_dotenv does not override an existing key, so this is what
    # makes the checkout's own .env unable to supply the identity.
    env["TEZ_PROXMOX_ENDPOINT"] = ""
    env["TEZ_PROXMOX_API_TOKEN"] = ""
    return subprocess.run([sys.executable, str(REPO / script), *argv],
                          capture_output=True, text=True, cwd=cwd, env=env, timeout=60)


class StartupRefusal(unittest.TestCase):
    def _assert_refused(self, script, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            out = _run(script, *argv, cwd=tmp)
        text = out.stdout + out.stderr
        self.assertNotEqual(out.returncode, 0, text)
        self.assertIn("proxmox.endpoint", text)
        self.assertIn("tezcatlipoca.toml", text)

    def test_create_competition_refuses(self):
        self._assert_refused("create-competition.py", "--competition", "probe", "--yes")

    def test_destroy_competition_refuses(self):
        self._assert_refused("destroy-competition.py", "--comp-dir", "/nonexistent", "--yes")

    def test_redeploy_competition_refuses(self):
        self._assert_refused("redeploy-competition.py", "--comp-dir", "/nonexistent", "--yes")

    def test_verify_competition_refuses(self):
        self._assert_refused("verify-competition.py", "/nonexistent")

    def test_plan_only_is_exempt_because_it_touches_no_infrastructure(self):
        with tempfile.TemporaryDirectory() as tmp:
            comp = Path(tmp) / "comp"
            comp.mkdir()
            (comp / "Compfile").write_text("name probe\nscenario s\ndifficulty 1\n")
            (comp / "boxes.json").write_text(
                '[{"name": "web01", "template": "t", "cpu": 1, "memory_mb": 1024}]')
            out = _run("create-competition.py", "--comp-dir", str(comp), "--plan-only",
                       cwd=tmp)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PLAN for 'comp'", out.stdout)


class IdentityHelper(unittest.TestCase):
    def test_one_resolved_key_is_enough(self):
        # The refusal is for an empty ambient config, not for a missing endpoint: nodes.json
        # supplies the endpoint, and a token alone still proves the estate is configured.
        from estate import config
        config.reset()
        self.addCleanup(config.reset)
        with mock.patch.dict(os.environ, {"TEZ_PROXMOX_API_TOKEN": "t"}, clear=True):
            config.require_proxmox_identity()
            self.assertEqual(config.active().proxmox.api_token, "t")


if __name__ == "__main__":
    unittest.main()
