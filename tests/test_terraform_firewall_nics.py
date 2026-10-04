"""Static guard: the in-path firewall clone must get BOTH NICs (net0 WAN on vmbrW<id>,
net1 LAN on the team bridge). Live-found 2026-10-04: the LAN block was gated on
`in_path ? [] : [...]`, so the firewall booted with only the WAN NIC."""
import re
from pathlib import Path

MAIN = (Path(__file__).resolve().parent.parent / "terraform" / "main.tf").read_text()


def _team_box_block():
    start = MAIN.index('resource "proxmox_virtual_environment_vm" "team_box" {')
    end = MAIN.index('resource "proxmox_virtual_environment_vm" "team_box_sat1"')
    return MAIN[start:end]


def test_firewall_gets_wan_then_lan_nic():
    blk = _team_box_block()
    fors = re.findall(r'dynamic "network_device" \{.*?for_each = ([^\n]+)', blk, re.S)
    assert len(fors) == 2
    assert "vmbrW" in fors[0] and "in_path" in fors[0]
    # the LAN/team-bridge NIC must not be suppressed for in_path boxes
    assert "in_path" not in fors[1] and "each.value.bridge" in fors[1]
