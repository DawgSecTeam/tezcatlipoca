"""Clone team1 boxes to other teams and reconfigure networking."""

import json
import os
import time

from constants import SNAP_BASE
from hardening_ops import fix_dns_on_boxes, fix_services_on_boxes, setup_ubuntu_auth
from range_ops import (
    enumerate_targets,
    guest_agent_exec_root,
    proxmox_api,
    stop_vm,
    take_snapshot,
    vm_id_for,
    wait_for_proxmox_task,
)
from ssh_ops import ssh_via_gateway, wait_for_boxes_ssh, wait_for_cloud_init
from windows_ops import bootstrap_windows_box, is_windows_template


def _agent_ipv4_present(node, vmid, expected_ip, wait_seconds=0):
    """True when the guest agent reports expected_ip on some interface. Agent
    exceptions count as 'not yet' — a box still booting its agent must not be
    'repaired' while cloud-init is simply slow."""
    deadline = time.time() + wait_seconds
    while True:
        try:
            ifaces = proxmox_api(
                "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
            )["data"]["result"]
            for iface in ifaces:
                for a in iface.get("ip-addresses", []):
                    if (a.get("ip-address-type") == "ipv4"
                            and a.get("ip-address") == expected_ip):
                        return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(10)


def _repair_box_network(node, vmid, expected_ip):
    """Re-add the box's address live and persist it against future blips.

    The e2e-2026-09-19 lesson (app01): clones can come up without the
    ipconfig0 address (cloud-init race) and ifupdown loses the address on any
    carrier blip. This is the repair that was never written down — it lives
    here now: re-add address + default route via the guest agent (root, no
    network or sudo needed), then persist a systemd-networkd .network with
    KeepConfiguration (keeps the address through carrier/link churn) and, for
    ifupdown systems, a static interfaces.d stanza. Both apply the same
    address, so coexisting is harmless.
    """
    cidr = f"{expected_ip}/24"
    gw = f"192.168.{expected_ip.split('.')[2]}.1"
    ifaces = proxmox_api(
        "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
    )["data"]["result"]
    iface = next((i["name"] for i in ifaces if i.get("name") and i["name"] != "lo"), None)
    if not iface:
        raise RuntimeError("no non-loopback interface reported by guest agent")
    script = f"""set -e
ip addr add {cidr} dev {iface} 2>/dev/null || true
ip link set {iface} up
ip route replace default via {gw} dev {iface} 2>/dev/null || true
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/90-tz-static.network <<'EOF'
[Match]
Name={iface}

[Network]
Address={cidr}
Gateway={gw}
DNS=8.8.8.8
KeepConfiguration=static
KeepConfiguration=dhcp
EOF
if [ -f /etc/network/interfaces ]; then
  mkdir -p /etc/network/interfaces.d
  cat > /etc/network/interfaces.d/90-tz-static <<'EOF'
auto {iface}
iface {iface} inet static
    address {cidr}
    gateway {gw}
EOF
fi
systemctl enable systemd-networkd >/dev/null 2>&1 || true
systemctl restart systemd-networkd >/dev/null 2>&1 || true
"""
    rc, out, err = guest_agent_exec_root(node, vmid, script, timeout=60)
    if rc != 0:
        raise RuntimeError(f"rc={rc}: {(err or out).strip()[:200]}")
    print(f"    {expected_ip}: address re-added; networkd .network (KeepConfiguration) persisted")


def ensure_cloned_network(teams, boxes):
    """Post-start IPv4 check + repair for every Linux box (D1/D2).

    Runs right after the start loop and before the SSH waits: a clone without
    a routable address would otherwise fail phase 6/7 an hour later. Windows
    boxes are skipped — bootstrap_windows_box does its own network config.
    """
    node = os.environ["TF_VAR_proxmox_node"]
    for team in teams:
        for box_idx, box in enumerate(boxes):
            if is_windows_template(box["template"]):
                continue
            expected = f"192.168.{team['identifier']}.{box['last_octet']}"
            vmid = vm_id_for(team["identifier"], box_idx)
            if _agent_ipv4_present(node, vmid, expected, wait_seconds=30):
                continue
            print(f"    {expected} (vmid {vmid}) missing expected IPv4 — repairing via guest agent")
            try:
                _repair_box_network(node, vmid, expected)
            except Exception as e:
                print(f"  WARNING: network repair failed for vmid {vmid}: {e} — "
                      f"wait_for_boxes_ssh will diagnose if it is still broken")


def clone_team_boxes(teams, boxes, ctx, comp_dir, box_creds=None, box_password=None):
    """Clone team1 boxes to other teams and configure networking.

    After cloning, reconfigures IP addresses, fixes DNS, and hardens services
    on ALL team boxes (team1 + cloned teams).
    """
    node = os.environ["TF_VAR_proxmox_node"]
    team_ids = list(teams.values())
    if len(team_ids) < 2:
        print("  Only one team — skipping box cloning.")
        return

    team1 = team_ids[0]

    # Step 1: cloud-init clean on team1 (Linux only; Windows uses sysprep).
    print("  Running cloud-init clean on team1 boxes...")
    for box in boxes:
        if is_windows_template(box["template"]):
            continue
        ip = f"192.168.{team1['identifier']}.{box['last_octet']}"
        try:
            result = ssh_via_gateway(ctx, ip, "sudo cloud-init clean --logs --machine-id",
                                      timeout=30, user=ctx.get("box_username", "ubuntu"))
            if result.returncode != 0:
                print(f"  WARNING: cloud-init clean FAILED for {box['name']} "
                      f"(rc={result.returncode}): {(result.stderr or '').strip()[:150]}")
                print(f"           Clones of {box['name']} may inherit its machine-id/"
                      f"cloud-init state (duplicate-identity bugs).")
            else:
                print(f"    {box['name']}: cloud-init clean done")
        except Exception as e:
            print(f"  WARNING: cloud-init clean failed for {box['name']}: {e}")

    # Step 2: Stop team1 boxes
    # Use 0-based box index for vm_id_for (Terraform creates VMs with 0-based index)
    # while last_octet is used for IP addresses
    print("  Shutting down team1 boxes...")
    for box_idx, box in enumerate(boxes):
        vmid = vm_id_for(team1["identifier"], box_idx)
        vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        vm = next((v for v in vms if v["vmid"] == vmid), None)
        if vm and vm.get("status") == "running":
            stop_vm(node, vmid)
        print(f"    team1-{box['name']} (vmid {vmid}) stopped")

    # Step 3: Clone team1 boxes for each subsequent team
    print("  Cloning team1 boxes to other teams...")
    # Track cloned VMIDs for destroy + resume (not in Terraform state).
    cloned_vms_path = comp_dir / "cloned_vms.json"
    cloned_vms = json.loads(cloned_vms_path.read_text()) if cloned_vms_path.exists() else {}
    existing_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
    for team in team_ids[1:]:
        for box_idx, box in enumerate(boxes):
            src_vmid = vm_id_for(team1["identifier"], box_idx)
            dst_vmid = vm_id_for(team["identifier"], box_idx)
            clone_name = f"{team['identifier']}-{box['name']}"

            if dst_vmid in existing_vmids:
                print(f"    {clone_name} (vmid {dst_vmid}) already exists — skipping clone (resume)")
            else:
                upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src_vmid}/clone", data={
                    "newid": dst_vmid,
                    "name": clone_name,
                    "full": 1,
                })["data"]
                print(f"    team1-{box['name']} (vmid {src_vmid}) -> {clone_name} (vmid {dst_vmid})...")
                wait_for_proxmox_task(node, upid)

            cloned_vms[clone_name] = dst_vmid
            cloned_vms_path.write_text(json.dumps(cloned_vms, indent=2))

            team_subnet = team["identifier"]
            box_octet = box["last_octet"]
            bridge = f"vmbr{team_subnet}"

            if is_windows_template(box["template"]):
                proxmox_api("PUT", f"/nodes/{node}/qemu/{dst_vmid}/config", data={
                    "net0": f"virtio,bridge={bridge}",
                })
            else:
                ipconfig = f"ip=192.168.{team_subnet}.{box_octet}/24,gw=192.168.{team_subnet}.1"
                proxmox_api("PUT", f"/nodes/{node}/qemu/{dst_vmid}/config", data={
                    "ipconfig0": ipconfig,
                    "net0": f"virtio,bridge={bridge}",
                })

    # Step 4: Start ALL team boxes (team1 + cloned)
    print("  Starting all team boxes...")
    for team in team_ids:
        for box_idx, box in enumerate(boxes):
            vmid = vm_id_for(team["identifier"], box_idx)
            try:
                vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                vm = next((v for v in vms if v["vmid"] == vmid), None)
                if vm and vm.get("status") != "running":
                    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")["data"]
                    wait_for_proxmox_task(node, upid, timeout=120)
                print(f"    vmid {vmid} ({team['identifier']}-{box['name']}) started")
            except Exception as e:
                print(f"  WARNING: Failed to start vmid {vmid}: {e}")

    # Step 4.5: every box must actually have its IPv4 before anything SSHes
    # into it — the cloud-init race / carrier-blip repair happens here instead
    # of failing phase 6/7 an hour later.
    ensure_cloned_network(teams, boxes)

    all_targets = enumerate_targets(teams, boxes)

    # Bootstrap cloned team2+ Windows boxes (team1 done in phase 4.5).
    for t in all_targets:
        if t["team_key"] == "team1" or not is_windows_template(t["box"]["template"]):
            continue
        print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
        gw = f"192.168.{t['identifier']}.1"
        bootstrap_windows_box(node, t["vmid"], t["ip"], gw, "8.8.8.8", box_password)

    wait_for_boxes_ssh(ctx, all_targets, timeout=300)
    wait_for_cloud_init(ctx, all_targets, timeout=240)

    # NOPASSWD sudo BEFORE the DNS fix: on fresh clones the DNS fix's sudo
    # soft-fails 8x per box until the sudoers grant lands (D3), and the
    # guest-agent fallback in fix_dns_on_boxes covers any remaining gap.
    setup_ubuntu_auth([t for t in all_targets if not is_windows_template(t["box"]["template"])], ctx)
    fix_dns_on_boxes([t for t in all_targets if not is_windows_template(t["box"]["template"])], ctx)

    # Snapshot cloned team2+ boxes before phase-6 nakon (team1 already has tz-base).
    print(f"  Snapshotting cloned boxes as '{SNAP_BASE}' (pre-Nakon restore point)...")
    for t in all_targets:
        if t["team_key"] == "team1":
            continue  # already snapshotted in phase [5/7]
        take_snapshot(node, t["vmid"], SNAP_BASE,
                      description="tezcatlipoca: cloned, networked, pre-Nakon")

    fix_services_on_boxes(
        comp_dir, [t for t in all_targets if not is_windows_template(t["box"]["template"])],
        ctx, box_creds=box_creds,
    )
