"""One definition per shared helper — the duplicates must not come back (audit 2026-10-02).

Each of these helpers existed two or three times with nothing enforcing that the copies
agreed. They are not cosmetic: is_windows_template decides Windows-vs-Linux for nakon's
machine routing, ssh_ops' gateway auth and deploy/golden_ops' target split; os_to_platform
is the same decision exposed as a platform name; and the generated secret's charset is
load-bearing (a letters+digits pool produced digit-free passwords that AD complexity policy
rejected on every Windows box, scrim-extreme-2026-09-20). A silent divergence in any of
them builds the wrong range. These guards fail if a second implementation reappears."""

import inspect
import re
import string
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import config_ops  # noqa: E402
import nakon_ops  # noqa: E402
import packet_ops  # noqa: E402
import ssh_ops  # noqa: E402
import windows_ops  # noqa: E402


def _definitions(name):
    """root *.py file -> number of top-level `def name` statements in it."""
    counts = {}
    for path in sorted(_REPO.glob("*.py")):
        found = re.findall(rf"^def {re.escape(name)}\b", path.read_text(), re.MULTILINE)
        if found:
            counts[path.name] = len(found)
    return counts


class WindowsTemplatePredicateTests(unittest.TestCase):
    """2a: is_windows_template is defined once, in windows_ops."""

    def test_every_call_site_is_the_same_function_object(self):
        self.assertIs(nakon_ops.is_windows_template, windows_ops.is_windows_template)
        self.assertIs(ssh_ops.is_windows_template, windows_ops.is_windows_template)

    def test_canonical_home_is_windows_ops(self):
        filename = inspect.getsourcefile(windows_ops.is_windows_template)
        self.assertEqual(Path(filename).name, "windows_ops.py")

    def test_exactly_one_definition_and_no_private_alias(self):
        self.assertEqual(_definitions("is_windows_template"), {"windows_ops.py": 1})
        self.assertEqual(_definitions("_is_windows_template"), {})

    def test_rule_is_unchanged(self):
        for template, expected in [("tmpl-ubuntu-22", False), ("", False), ("win", True),
                                   ("WIN-2022", True), ("windows-server-2022", True)]:
            self.assertIs(windows_ops.is_windows_template(template), expected)


class PlatformMapTests(unittest.TestCase):
    """2b: packet_ops._platform_of is a wrapper over the one map in nakon_ops."""

    def test_exactly_one_definition(self):
        self.assertEqual(_definitions("os_to_platform"), {"nakon_config_ops.py": 1})

    def test_packet_wrapper_agrees_on_every_string_input(self):
        for template in ("", "tmpl-ubuntu-22", "win", "WIN-SERVER", "windows-server-2022"):
            self.assertEqual(packet_ops._platform_of(template),
                             nakon_ops.os_to_platform(template))

    def test_wrapper_still_delegates(self):
        source = inspect.getsource(packet_ops._platform_of)
        self.assertIn("os_to_platform(str(template))", source)

    def test_documented_divergence_for_absent_template_is_intentional(self):
        """The one real difference between the old copies: packet YAML tolerates a box with
        no `template` key and classified it as linux, while the shared map calls .lower()
        straight on its argument. The coercion is packet_ops' and is preserved on purpose."""
        self.assertEqual(packet_ops._platform_of(None), "linux")
        with self.assertRaises(AttributeError):
            nakon_ops.os_to_platform(None)


class PasswordGeneratorTests(unittest.TestCase):
    """2c: one generator, config_ops.random_password; packet_ops._gen_password aliases it."""

    def test_packet_alias_is_the_config_ops_function(self):
        self.assertIs(packet_ops._gen_password, config_ops.random_password)

    def test_exactly_one_implementation(self):
        self.assertEqual(_definitions("random_password"), {"config_ops.py": 1})
        self.assertEqual(_definitions("_gen_password"), {})

    def test_charset_and_guarantees_are_unchanged(self):
        """Pin the behaviour the incidents hung on: 14 chars, every character class
        represented, nothing outside the cmd/PS- and URL-safe alphabet. A forbidden
        character in the pool shows up in ~700 draws essentially immediately."""
        allowed = set(string.ascii_uppercase + string.ascii_lowercase + string.digits
                      + "!*_-+=")
        pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!*_-+="]
        seen = set()
        for _ in range(50):
            password = config_ops.random_password()
            self.assertEqual(len(password), 14)
            self.assertTrue(set(password) <= allowed, password)
            for pool in pools:
                self.assertTrue(any(ch in pool for ch in password), password)
            seen |= set(password)
        for forbidden in "#/?@ &|<>^%$`\"'":
            self.assertNotIn(forbidden, seen,
                             f"{forbidden!r} must never be drawn (DSN/quoting hazard)")


if __name__ == "__main__":
    unittest.main()
