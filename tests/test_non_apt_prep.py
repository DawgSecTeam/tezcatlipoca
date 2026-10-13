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
from guests.alpine_ops import ALPINE_SERVICES
from guests.apt_dns_ops import _APT_PREP_BODY
from guests.box_settle_ops import _SETTLE_CHECK


class AlpineShim(unittest.TestCase):
    def test_map_covers_the_realistic_service_pins(self):
        for svc in ("nginx", "apache", "bind"):
            pkg, openrc = ALPINE_SERVICES[svc]
            self.assertTrue(pkg and openrc)


class NonAptPrep(unittest.TestCase):
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
