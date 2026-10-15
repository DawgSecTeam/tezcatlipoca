"""Guest-side engine configuration and the cold boot that makes its NICs appear.

Replaces terraform's three null_resources (main.tf: team_nics, reboot_scoring_engine,
orchestrate). They were remote-exec/local-exec scripts inside a declarative tool; here they
are ordinary calls the deploy phase makes in the same order.
"""

import time

from common.ssh_ops import ssh_on_gateway, wait_for_ssh
from infra.client import proxmox_api, wait_for_proxmox_task

# The hard-stop settle terraform used between stop and start: long enough for the VM to be
# fully down, short enough not to matter (main.tf slept 15).
COLD_BOOT_SETTLE_S = 15
# The readiness probe's tail settle (main.tf's orchestrate slept 30 after the last probe).
READY_SETTLE_S = 30


def cold_boot(node, vmid):
    """Hard-stop then start the engine.

    A guest reboot does NOT trigger the PCI rescan that reveals newly added VirtIO NICs —
    only a cold boot does — so this must run after the NICs are in the VM config and before
    the in-guest netplan that addresses them.
    """
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
    wait_for_proxmox_task(node, upid)
    time.sleep(COLD_BOOT_SETTLE_S)
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")["data"]
    wait_for_proxmox_task(node, upid)


def wait_engine_ready(ctx, timeout=300, settle=READY_SETTLE_S):
    """Wait for the engine to answer SSH, then let it settle before nakon dials the boxes."""
    if not wait_for_ssh(ctx["ssh_key_path"], ctx["vm_username"], ctx["scoring_engine_ip"],
                        timeout=timeout):
        raise RuntimeError(f"scoring engine {ctx['scoring_engine_ip']} never accepted SSH")
    time.sleep(settle)


def satellite_route_commands(routes):
    """The engine's static routes to each satellite's team subnets, plus the oneshot unit
    that re-asserts them: an engine reboot mid-event would otherwise strand satellite
    scoring with no route to reach it."""
    commands = [f"ip route replace {r['subnet']} via {r['via']} || true" for r in routes]
    script_lines = ["#!/bin/sh", *commands]
    commands_quoted = " ".join(f"'{line}'" for line in script_lines)
    unit = " ".join(f"'{line}'" for line in (
        "[Unit]",
        "Description=Tezcatlipoca satellite team routes",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=/usr/local/sbin/satellite-routes.sh",
        "RemainAfterExit=yes",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ))
    return [
        f"printf '%s\\n' {commands_quoted} | sudo tee /usr/local/sbin/satellite-routes.sh > /dev/null",
        "sudo chmod +x /usr/local/sbin/satellite-routes.sh",
        f"printf '%s\\n' {unit} | sudo tee /etc/systemd/system/satellite-routes.service > /dev/null",
        "sudo systemctl daemon-reload",
        "sudo systemctl enable --now satellite-routes.service",
    ]


def netplan_yaml(team_identifiers, transit_identifiers=()):
    """The engine's team-interface netplan.

    Addresses follow the NIC order create_engine built: net0 is the mgmt bridge (vmbr0,
    addressed by the image), then one NIC per slot-0 team bridge, then the in-path transit
    NICs — so team k is ens19+k and transit k appends after every team NIC.
    """
    lines = ["network:", "  version: 2", "  ethernets:"]
    for index, identifier in enumerate(team_identifiers):
        lines.append(f'    ens{18 + index + 1}:\n      addresses: ["192.168.{identifier}.1/24"]')
    for index, identifier in enumerate(transit_identifiers):
        lines.append(
            f'    ens{18 + len(team_identifiers) + index + 1}:\n'
            f'      addresses: ["172.31.{identifier}.1/30"]')
    return "\n".join(lines)


def configure_engine_network(ctx, team_identifiers, transit_identifiers=(), satellite_routes=()):
    """Address the engine's team bridges and enable forwarding, over SSH.

    The netplan block is omitted when the engine hosts no slot-0 teams (an all-satellite
    spread has no local bridges) — an empty ethernets map is a netplan error.
    """
    if team_identifiers:
        _run(ctx, "sudo tee /etc/netplan/60-team-ifaces.yaml << 'EOF'\n"
                  + netplan_yaml(team_identifiers, transit_identifiers) + "\nEOF")
        # netplan refuses a world-readable file.
        _run(ctx, "sudo chmod 600 /etc/netplan/60-team-ifaces.yaml")
        _run(ctx, "sudo netplan apply")
    _run(ctx, "printf 'net.ipv4.ip_forward=1\\nnet.ipv4.conf.all.rp_filter=2\\n"
              "net.ipv4.conf.default.rp_filter=2\\n' | sudo tee /etc/sysctl.d/99-range-forward.conf")
    _run(ctx, "sudo sysctl -p /etc/sysctl.d/99-range-forward.conf")
    if satellite_routes:
        for command in satellite_route_commands(satellite_routes):
            _run(ctx, command)


def _run(ctx, command):
    result = ssh_on_gateway(ctx, command, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"engine setup command failed (rc={result.returncode}): {command}\n"
                           f"{(result.stderr or '').strip()[:400]}")
