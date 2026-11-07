"""prep_apt's channel: SSH retry is the primary everywhere; the guest agent is a
proxmox-only channel — on gcp its pre-dial is a dead 120s stall (live, gcp-acc14)."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from guests import apt_dns_ops  # noqa: E402

GCP_CTX = {"platform": "gcp", "box_username": "ubuntu", "scoring_engine_ip": "10.0.0.9"}
TARGET = {"ip": "192.168.101.10", "vmid": 1210, "identifier": "101",
          "node": "us-central1-a"}


class AptPrepChannel(unittest.TestCase):
    def test_gcp_never_waits_on_the_guest_agent(self):
        prepared = []

        def retry(ctx, ip, user, cmd, **kw):
            prepared.append(ip)
            return True, ""

        with patch.object(apt_dns_ops, "wait_boxes_settled", return_value=set()), \
                patch.object(apt_dns_ops, "wait_for_guest_agent",
                             side_effect=AssertionError("agent dialed on gcp")), \
                patch.object(apt_dns_ops, "box_ssh_retry", side_effect=retry):
            apt_dns_ops.prep_apt_on_boxes([TARGET], GCP_CTX)

        self.assertEqual(prepared, [TARGET["ip"]])

    def test_proxmox_waits_on_the_agent_before_the_ssh_retry(self):
        dialed = []

        def agent_wait(node, vmid, timeout=0):
            dialed.append((node, vmid, timeout))
            return True

        def retry(ctx, ip, user, cmd, **kw):
            return True, ""

        with patch.object(apt_dns_ops, "wait_boxes_settled", return_value=set()), \
                patch.object(apt_dns_ops, "wait_for_guest_agent",
                             side_effect=agent_wait), \
                patch.object(apt_dns_ops, "box_ssh_retry", side_effect=retry):
            apt_dns_ops.prep_apt_on_boxes([TARGET], {"box_username": "ubuntu"})

        self.assertEqual(dialed, [(TARGET["node"], TARGET["vmid"], 120)])

    def test_unsettled_warning_names_vmids(self):
        # wait_boxes_settled returns vmids; the pre-fix detail line did t.get("ip")
        # over those ints and crashed the deploy the moment a box never settled
        # (live, gcp-acc14 — the unsettled path is reachable on proxmox too).
        with patch.object(apt_dns_ops, "wait_boxes_settled",
                          return_value={TARGET["vmid"]}), \
                patch.object(apt_dns_ops, "record_degradation") as recorded, \
                patch.object(apt_dns_ops, "box_ssh_retry", return_value=(True, "")):
            apt_dns_ops.prep_apt_on_boxes([TARGET], GCP_CTX)

        self.assertEqual(recorded.call_args[0][0], "boxes never settled after boot")
        self.assertEqual(recorded.call_args[0][1], str(TARGET["vmid"]))


if __name__ == "__main__":
    unittest.main()
