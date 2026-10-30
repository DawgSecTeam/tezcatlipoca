"""The box-hop ssh argv builders: one place now spells the gateway option set that
used to be hand-rebuilt in ssh_via_gateway, classify_ssh_failure, apt_dns_ops,
ubuntu_auth_ops and beacon_ops."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from common import ssh_ops  # noqa: E402

CTX = {"ssh_key_path": "/k", "scoring_engine_ip": "10.0.0.9", "vm_username": "ubuntu"}
PROXY = "ssh -i /k -W %h:%p ubuntu@10.0.0.9"


class BoxSshArgv(unittest.TestCase):


    def test_argv_prefixes_ssh_i_and_targets_user_at_host(self):
        with patch.object(ssh_ops, "gateway_proxy", return_value=PROXY):
            argv = ssh_ops.box_ssh_argv(CTX, "192.168.1.5", "ubuntu", "whoami")
        self.assertEqual(
            argv,
            ["ssh", "-i", "/k",
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=10",
             "-o", f"ProxyCommand={PROXY}",
             "ubuntu@192.168.1.5", "whoami"],
        )

    def test_argv_omits_remote_when_none(self):
        with patch.object(ssh_ops, "gateway_proxy", return_value=PROXY):
            argv = ssh_ops.box_ssh_argv(CTX, "192.168.1.5", "ubuntu")
        self.assertEqual(argv[-1], "ubuntu@192.168.1.5")


class GatewayProxyEscaping(unittest.TestCase):
    # OpenSSH 10 percent-expands a ProxyCommand string itself and makes an escape its
    # ProxyCommand table lacks fatal (`vdollar_percent_expand: unknown key %C`). %C belongs to
    # ControlPath, so a single % killed every box hop in ~10ms with rc 255 and empty stdout.
    # The doubled form survives one outer expansion as the literal %C the inner ssh needs.

    def setUp(self):
        self.proxy = ssh_ops.gateway_proxy(CTX)

    def test_controlpath_percent_arrives_doubled(self):
        self.assertIn("cm-%%C", self.proxy)
        self.assertNotIn("cm-%C", self.proxy)

    def test_one_outer_expansion_leaves_a_valid_inner_controlpath(self):
        inner = self.proxy.replace("%%", "%")
        self.assertIn("ControlPath=", inner)
        self.assertIn("cm-%C", inner)
        self.assertNotIn("%%", inner)

    def test_jump_escapes_for_the_inner_w_are_left_for_ssh(self):
        self.assertIn("-W %h:%p", self.proxy)



if __name__ == "__main__":
    unittest.main()
