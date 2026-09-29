"""box_username legacy-account lint (distro-matrix-2026-09-27: box_username operator
collides with the uid-11 legacy `operator` account; cloud-init adopts it, the sudoers
rule and SSH key land on a nologin root-homed shadow, and phase 4's auth ladder burns
out). Both users.json paths reject the name. Pure functions + tempfile users.json."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
import config_ops
import utils


class LegacyAccountNames(unittest.TestCase):
    def test_known_landmines_blocked(self):
        for name in ("operator", "daemon", "games", "mail", "news", "sync", "root"):
            self.assertTrue(utils.is_legacy_account_name(name), name)

    def test_proven_safe_names_pass(self):
        for name in ("medic", "engineer", "sysadmin", "analyst1", "ubuntu"):
            self.assertFalse(utils.is_legacy_account_name(name), name)


class UsersJsonPath(unittest.TestCase):
    def _load(self, box_username):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "users.json").write_text(json.dumps({"box_username": box_username}))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                got = utils.load_users_config(d)
        return got, stdout.getvalue()

    def test_operator_falls_back_with_warning(self):
        (box_username, _), out = self._load("operator")
        self.assertEqual(utils.BOX_USERNAME_DEFAULT, box_username)
        self.assertIn("legacy distro system account", out)

    def test_safe_name_passes_through_silently(self):
        (box_username, _), out = self._load("medic")
        self.assertEqual("medic", box_username)
        self.assertEqual("", out)


class GenerateTimePath(unittest.TestCase):
    def test_flag_operator_falls_back_with_warning(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            box_username, _ = config_ops.collect_users_config(
                box_username_flag="operator", credlist_flag="admin,user1,user2")
        self.assertEqual(utils.BOX_USERNAME_DEFAULT, box_username)
        self.assertIn("legacy distro system account", stdout.getvalue())

    def test_flag_medic_kept(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            box_username, _ = config_ops.collect_users_config(
                box_username_flag="medic", credlist_flag="admin,user1,user2")
        self.assertEqual("medic", box_username)
        self.assertEqual("", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
