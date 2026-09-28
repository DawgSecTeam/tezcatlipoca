"""apt-prep / settle scripts on non-apt distros (fedora, alpine): honest fast no-ops.
Offline; runs the actual script bodies with a PATH that has no commands at all."""

import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from hardening_ops import _APT_PREP_BODY, _SETTLE_CHECK


class NonAptPrep(unittest.TestCase):
    def test_prep_body_guards_before_any_apt_call(self):
        body = _APT_PREP_BODY.lstrip()
        self.assertTrue(body.startswith("set +e\n"))
        guard = "command -v apt-get >/dev/null 2>&1 || exit 0"
        first_apt = body.find("apt-get")
        self.assertGreater(first_apt, -1)
        self.assertLess(body.find(guard), first_apt,
                        "apt-guard must precede the first apt-get invocation")

    def test_settle_check_guards_before_dpkg_probe(self):
        check = _SETTLE_CHECK.lstrip()
        guard = "command -v apt-get"
        first_probe = check.find("apt-get -o")
        self.assertGreater(first_probe, -1)
        self.assertLess(check.find(guard), first_probe,
                        "settle guard must precede the apt-get check probe")

    @classmethod
    def setUpClass(cls):
        cls.empty_bin = tempfile.mkdtemp(prefix="tz-non-apt-path-")

    def _run_body(self, body):
        # /bin/sh exec'd absolutely (the child PATH is an empty dir), so the
        # script body itself sees a world without sh, systemctl, or apt-get.
        script = f"export PATH={shlex.quote(self.empty_bin)}\n" + body
        return subprocess.run(["/bin/sh", "-c", script],
                              capture_output=True, text=True, timeout=30)

    def test_prep_is_rc0_noop_without_apt(self):
        r = self._run_body(_APT_PREP_BODY)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")

    def test_settle_reports_settled_without_apt(self):
        r = self._run_body(_SETTLE_CHECK)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "SETTLED\n")


if __name__ == "__main__":
    unittest.main()
