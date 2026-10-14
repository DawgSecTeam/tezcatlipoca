"""The range's ssh key must come from the RUN's configuration, never from a checkout path.

Regression for the 2026-10-14 e2e finding: `scrim/staging.py` hardcoded
`<engine checkout>/proxmox` and `stage_red` never pinned `deploy.ssh_key` on the config it builds
from scratch, so under a central run (engine = a per-run WORKTREE with no key in it) bad-auto fell
back to its built-in default — a SIBLING tezcatlipoca checkout — and `badauto deploy` died with
FileNotFoundError '/home/hna/tezcatlipoca/proxmox.pub' AFTER the range had been built.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from red import badauto_config_ops  # noqa: E402
from scrim import core  # noqa: E402


class SshKeyPath(unittest.TestCase):
    def test_absolute_env_value_wins(self):
        with patch.dict(os.environ, {"TF_VAR_ssh_private_key_path": "/home/hna/tez-proxmox"}):
            self.assertEqual(core.ssh_key_path(), "/home/hna/tez-proxmox")

    def test_relative_env_value_resolves_under_the_terraform_workdir(self):
        with patch.dict(os.environ, {"TF_VAR_ssh_private_key_path": "../proxmox"}):
            self.assertEqual(core.ssh_key_path(), str((_REPO / "proxmox").resolve()))

    def test_no_env_value_falls_back_to_the_checkout(self):
        env = {k: v for k, v in os.environ.items() if k != "TF_VAR_ssh_private_key_path"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(core.ssh_key_path(), str((_REPO / "proxmox").resolve()))


class PinDeployKey(unittest.TestCase):
    def test_pins_the_resolved_absolute_path(self):
        cfg = {"deploy": {"red_vmid": 999}, "realm": {}}
        badauto_config_ops.pin_deploy_key(cfg, "/home/hna/tez-proxmox")
        self.assertEqual(cfg["deploy"]["ssh_key"], "/home/hna/tez-proxmox")
        self.assertEqual(cfg["deploy"]["red_vmid"], 999, "sibling keys must survive")

    def test_no_key_leaves_the_config_alone(self):
        cfg = {"deploy": {}}
        badauto_config_ops.pin_deploy_key(cfg, "")
        self.assertNotIn("ssh_key", cfg["deploy"])

    def test_a_relative_key_becomes_absolute(self):
        cfg = {"deploy": {}}
        with patch("os.getcwd", return_value=str(_REPO)):
            badauto_config_ops.pin_deploy_key(cfg, "proxmox")
        self.assertTrue(Path(cfg["deploy"]["ssh_key"]).is_absolute())


class RedStagePinsIt(unittest.TestCase):
    def test_stage_red_source_pins_the_key_on_the_config_it_builds(self):
        """The config stage_red writes is built from scratch, so the pin must be in that path —
        this is the exact site that failed on a run host (the sibling-checkout fallback)."""
        src = (_REPO / "scrim" / "red_stage.py").read_text()
        self.assertIn("pin_deploy_key(cfg", src)


if __name__ == "__main__":
    unittest.main()
