"""estate.config: the native config surface that replaces TF_VAR_*/.env."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from estate import config


def _isolated_env():
    """Blank every key's env names: another test's leaked TF_VAR_*/TEZ_* must not reach a
    default assertion. Empty (not removed) reads as unset — see Config.get."""
    names = []
    for key, (_, legacy) in config._SPEC.items():
        names.append(config.env_name(key))
        names.extend(legacy)
    return mock.patch.dict(os.environ, {name: "" for name in names})


class Defaults(unittest.TestCase):
    def setUp(self):
        patch = _isolated_env()
        patch.start()
        self.addCleanup(patch.stop)

    def test_missing_file_yields_defaults(self):
        cfg = config.Config(Path("/nonexistent/tezcatlipoca.toml"))
        self.assertEqual(cfg.proxmox.node, "pve")
        self.assertEqual(cfg.proxmox.datastore, "local-lvm")
        self.assertEqual(cfg.engine.username, "sysadmin")
        self.assertEqual(cfg.headscale.infra_user, "cyberrange-infra")
        self.assertEqual(cfg.knobs.thin_headroom, 1.0)
        self.assertFalse(cfg.portal.open)

    def test_unknown_key_is_an_attribute_error(self):
        cfg = config.Config(Path("/nonexistent/tezcatlipoca.toml"))
        with self.assertRaises(AttributeError):
            cfg.proxmox.nope

    def test_bad_toml_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text("[proxmox\nendpoint = 1\n")
            with self.assertRaises(SystemExit) as raised:
                config.Config(path)
            self.assertIn(str(path), str(raised.exception))


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

    def test_file_values_are_read(self):
        self.assertEqual(self.cfg.proxmox.endpoint, "https://file.example:8006")
        self.assertEqual(self.cfg.proxmox.template_vm_id, 920)
        self.assertEqual(self.cfg.knobs.thin_headroom, 0.25)
        self.assertEqual(self.cfg.knobs.cycle_timeout, 900)
        self.assertTrue(self.cfg.portal.open)

    def test_file_values_are_coerced_to_the_default_type(self):
        self.assertIsInstance(self.cfg.proxmox.template_vm_id, int)
        self.assertIsInstance(self.cfg.knobs.thin_headroom, float)

    def test_env_beats_file(self):
        with mock.patch.dict(os.environ, {"TEZ_PROXMOX_ENDPOINT": "https://env.example:8006"}):
            self.assertEqual(self.cfg.proxmox.endpoint, "https://env.example:8006")

    def test_canonical_env_name_drops_the_knobs_prefix(self):
        with mock.patch.dict(os.environ, {"TEZ_THIN_HEADROOM": "0.5"}):
            self.assertEqual(self.cfg.knobs.thin_headroom, 0.5)

    def test_legacy_tf_var_name_still_resolves(self):
        with mock.patch.dict(os.environ, {"TF_VAR_datastore": "legacy-store"}):
            self.assertEqual(self.cfg.proxmox.datastore, "legacy-store")

    def test_legacy_portal_name_still_resolves(self):
        with mock.patch.dict(os.environ, {"PORTAL_OPEN": "1"}):
            self.assertTrue(self.cfg.portal.open)

    def test_empty_env_falls_through_to_the_file(self):
        with mock.patch.dict(os.environ, {"TF_VAR_datastore": ""}):
            self.assertEqual(self.cfg.proxmox.datastore, "file-store")

    def test_require_names_the_key_and_the_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.Config(Path(tmp) / "absent.toml")
            with self.assertRaises(SystemExit) as raised:
                cfg.require("proxmox.endpoint")
            message = str(raised.exception)
            self.assertIn("proxmox.endpoint", message)
            self.assertIn("TEZ_PROXMOX_ENDPOINT", message)

    def test_require_returns_a_set_value(self):
        with mock.patch.dict(os.environ, {"TF_VAR_proxmox_api_token": "root@pam!x=secret"}):
            self.assertEqual(self.cfg.require("proxmox.api_token"), "root@pam!x=secret")

    def test_is_set_distinguishes_unset_from_deliberately_empty(self):
        # `get` cannot: both read as the default. One caller needs the difference — an empty
        # engine mgmt IP means "DHCP" and must not be re-defaulted to a static address.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TF_VAR_engine_mgmt_ip", None)
            os.environ.pop("TEZ_ENGINE_MGMT_IP", None)
            self.assertFalse(self.cfg.is_set("engine.mgmt_ip"))
            with mock.patch.dict(os.environ, {"TF_VAR_engine_mgmt_ip": ""}):
                self.assertTrue(self.cfg.is_set("engine.mgmt_ip"))

    def test_is_set_sees_a_file_value(self):
        self.assertTrue(self.cfg.is_set("proxmox.datastore"))

    def test_require_is_also_a_module_level_function(self):
        with mock.patch.dict(os.environ, {"TF_VAR_proxmox_api_token": "root@pam!x=secret"}):
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

    def test_deactivation_restores_the_previous_override(self):
        self.cfg.activate({"proxmox.node": "first"})
        token = self.cfg.activate({"proxmox.node": "second"})
        self.assertEqual(self.cfg.proxmox.node, "second")
        self.cfg.deactivate(token)
        self.assertEqual(self.cfg.proxmox.node, "first")


class Singleton(unittest.TestCase):
    def tearDown(self):
        config.reset()

    def test_configure_then_active_returns_the_same_object(self):
        cfg = config.configure(Path("/nonexistent/tezcatlipoca.toml"))
        self.assertIs(config.active(), cfg)

    def test_env_selects_the_profile(self):
        with mock.patch.dict(os.environ, {config.CONFIG_ENV: "/tmp/profile.toml"}):
            self.assertEqual(config.active().path, Path("/tmp/profile.toml"))

    def test_env_name_derivation(self):
        self.assertEqual(config.env_name("proxmox.endpoint"), "TEZ_PROXMOX_ENDPOINT")
        self.assertEqual(config.env_name("knobs.thin_headroom"), "TEZ_THIN_HEADROOM")


if __name__ == "__main__":
    unittest.main()
