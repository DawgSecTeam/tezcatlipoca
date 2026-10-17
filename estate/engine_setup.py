"""Guest-side engine configuration and the cold boot that makes its NICs appear."""

import time

from common.ssh_ops import ssh_on_gateway, wait_for_ssh
from infra.client import proxmox_api, wait_for_proxmox_task


def engine_ctx(comp_dir):
    from estate import config, state
    flat = state.load_flat(comp_dir)
    return {"scoring_engine_ip": flat.get("engine_mgmt_ip") or "",
            "ssh_key_path": str(config.ssh_key_path()),
            "vm_username": config.active().engine.username}

# Hard-stop settle between stop and start: long enough for the VM to be fully down.
COLD_BOOT_SETTLE_S = 15
# Readiness probe's tail settle before nakon dials the boxes.
READY_SETTLE_S = 30


def cold_boot(node, vmid):
    """Hard-stop then start: only a cold boot triggers the PCI rescan that reveals newly added VirtIO NICs."""
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
    wait_for_proxmox_task(node, upid)
    time.sleep(COLD_BOOT_SETTLE_S)
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")["data"]
    wait_for_proxmox_task(node, upid)


def wait_engine_ready(ctx, timeout=300, settle=READY_SETTLE_S):
    if not wait_for_ssh(ctx["ssh_key_path"], ctx["vm_username"], ctx["scoring_engine_ip"],
                        timeout=timeout):
        raise RuntimeError(f"scoring engine {ctx['scoring_engine_ip']} never accepted SSH")
    time.sleep(settle)


def satellite_route_commands(routes):
    """Static routes to each satellite's team subnets, plus the oneshot unit that re-asserts them after an engine reboot."""
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


def netplan_yaml(team_identifiers, transit_identifiers=(), red_segment=None):
    """The engine's team-interface netplan; addresses follow create_engine's NIC order (mgmt, teams, transit, then red_segment)."""
    lines = ["network:", "  version: 2", "  ethernets:"]
    for index, identifier in enumerate(team_identifiers):
        lines.append(f'    ens{18 + index + 1}:\n      addresses: ["192.168.{identifier}.1/24"]')
    for index, identifier in enumerate(transit_identifiers):
        lines.append(
            f'    ens{18 + len(team_identifiers) + index + 1}:\n'
            f'      addresses: ["172.31.{identifier}.1/30"]')
    if red_segment:
        interface = 18 + len(team_identifiers) + len(transit_identifiers) + 1
        gw = red_segment["gw"]
        lines.append(f'    ens{interface}:\n      addresses: ["{gw}/24"]')
    return "\n".join(lines)


def configure_engine_network(ctx, team_identifiers, transit_identifiers=(), satellite_routes=(),
                             red_segment=None):
    """Address the engine's team bridges and enable forwarding over SSH; the netplan block is omitted when there are no local teams (an empty ethernets map is a netplan error)."""
    if team_identifiers:
        _run(ctx, "sudo tee /etc/netplan/60-team-ifaces.yaml << 'EOF'\n"
                  + netplan_yaml(team_identifiers, transit_identifiers, red_segment) + "\nEOF")
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
