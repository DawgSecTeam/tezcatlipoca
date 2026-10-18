"""estate.config: the native config surface (tezcatlipoca.toml plus TEZ_* env overrides)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from estate import config


def _isolated_env():
    """Blank every key's env name: another test's leaked TEZ_* must not reach a default
    assertion. Empty (not removed) reads as unset — see Config.get."""
    names = [config.env_name(key) for key in config._SPEC]
    return mock.patch.dict(os.environ, {name: "" for name in names})




class Resolution(unittest.TestCase):
    def setUp(self):
        patch = _isolated_env()
        patch.start()
        self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tezcatlipoca.toml"
        self.path.write_text(
            "[proxmox]\n"
            'endpoint = "https://file.example:8006"\n'
            'node = "file-node"\n'
            'datastore = "file-store"\n'
            "template_vm_id = 920\n"
            "\n[knobs]\n"
            "thin_headroom = 0.25\n"
            "cycle_timeout = 900\n"
            "\n[portal]\n"
            "open = true\n"
        )
        self.cfg = config.Config(self.path)



    def test_env_beats_file(self):
        with mock.patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": "https://env.example:8006"}):
            self.assertEqual(self.cfg.proxmox.endpoint, "https://env.example:8006")

    def test_canonical_env_name_drops_the_knobs_prefix(self):
        with mock.patch.dict(os.environ, {"TEZ_THIN_HEADROOM": "0.5"}):
            self.assertEqual(self.cfg.knobs.thin_headroom, 0.5)


    def test_a_missing_key_is_catchable_by_except_exception(self):
        # The old read was os.environ[...] (a KeyError), so tolerant callers wrap it in
        # `except Exception`; a bare SystemExit escapes those and kills the run instead.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(Path(tmp) / "absent.toml")
            with self.assertRaises(Exception) as raised:
                cfg.require("proxmox.endpoint")
            self.assertIsInstance(raised.exception, SystemExit)



    def test_is_set_distinguishes_unset_from_deliberately_empty(self):
        # `get` cannot: both read as the default. One caller needs the difference — an empty
        # engine mgmt IP means "DHCP" and must not be re-defaulted to a static address.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEZ_ENGINE_MGMT_IP", None)
            os.environ.pop("TEZ_ENGINE_MGMT_IP", None)
            self.assertFalse(self.cfg.is_set("engine.mgmt_ip"))
            with mock.patch.dict(os.environ, {"TEZ_ENGINE_MGMT_IP": ""}):
                self.assertTrue(self.cfg.is_set("engine.mgmt_ip"))


    def test_require_is_also_a_module_level_function(self):
        with mock.patch.dict(os.environ, {"TEZ_PROXMOX_API_TOKEN": "root@pam!x=secret"}):
            config.reset()
            self.addCleanup(config.reset)
            self.assertEqual(config.require("proxmox.api_token"), "root@pam!x=secret")


class Activation(unittest.TestCase):
    def setUp(self):
        self.cfg = config.Config(Path("/nonexistent/tezcatlipoca.toml"))

    def test_activation_beats_env(self):
        with mock.patch.dict(os.environ, {"TEZ_PROXMOX_NODE": "env-node"}):
            token = self.cfg.activate({"proxmox.node": "sat-node"})
            self.assertEqual(self.cfg.proxmox.node, "sat-node")
            self.cfg.deactivate(token)
            self.assertEqual(self.cfg.proxmox.node, "env-node")





class SshKeyPath(unittest.TestCase):
    """A relative profile path resolves against the checkout root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "proxmox").write_text("key")
        patch = mock.patch.object(config, "REPO_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        config.reset()
        self.addCleanup(config.reset)

    def _resolve(self, value):
        with mock.patch.dict(os.environ, {"TEZ_SSH_PRIVATE_KEY_PATH": value}):
            return config.ssh_key_path()


    def test_a_repo_relative_path_resolves_against_the_checkout(self):
        self.assertEqual(self._resolve("proxmox"), self.root / "proxmox")


if __name__ == "__main__":
    unittest.main()
