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



if __name__ == "__main__":
    unittest.main()
