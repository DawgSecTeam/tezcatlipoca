#!/usr/bin/env python3
"""Insert an in-path pfSense firewall in front of each team, AFTER a normal tezcatlipoca
deploy of the AD boxes (dc01/web01/app01). Run from the repo root with the competition's
.env loaded (TF_VAR_proxmox_* etc.).

Per team <id> it builds this topology (boxes keep their IPs/gateway, so no box changes):

    engine ──vmbrW<id> (172.31.<id>.1/30)── pfSense WAN(172.31.<id>.2/30)
                                              pfSense LAN(192.168.<id>.1/24) ──vmbr<id>── boxes

  - engine: add NIC on vmbrW<id> = 172.31.<id>.1/30, route 192.168.<id>.0/24 via .2, and
    REMOVE its 192.168.<id>.1 address from vmbr<id> (pfSense owns .1 now). Engine keeps its
    MASQUERADE for 192.168.0.0/16 -> internet, so boxes still reach the internet via pfSense
    (pure router) -> engine (NAT).
  - pfSense: full clone of the `pfsense-fix` template, 2 NICs, per-team config.xml injected
    offline (gen_pfsense_config.py), booted. NAT disabled (engine NATs); WAN pass rule lets
    the engine's routed scoring reach the LAN.

PREREQUISITES (see MORNING-REPORT.md): host-write permission (config injection uses ssh to
root@<host> + zpool) and `pfsense-fix` templated. This script is idempotent per team.

NOTE: this is the standalone inserter matching the plan's "unmanaged box built by a separate
mechanism, not the core team_box terraform". It has NOT been run end-to-end yet (deploy was
blocked overnight) — validate the first team before fanning out to all four.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import range_ops  # noqa: E402

HERE = Path(__file__).resolve().parent
NODE = os.environ["TF_VAR_proxmox_node"]
KEY = str((Path(__file__).resolve().parents[3] / "proxmox"))
HOST = os.environ["TF_VAR_proxmox_endpoint"].split("//")[1].split(":")[0]
PF_TEMPLATE_NAME = "pfsense-fix"
# pfSense per-team vmid: sits in the team block just above the boxes (team base + 8).
def pf_vmid(identifier):
    return 200 + int(identifier) * 10 + 8


def sh_host(cmd):
    """Run a shell command on the Proxmox host as root (needs host-write permission)."""
    return subprocess.run(
        ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         f"root@{HOST}", cmd], capture_output=True, text=True, timeout=300)


def ssh_engine(engine_ip, cmd):
    user = os.environ.get("TF_VAR_vm_username", "sysadmin")
    return subprocess.run(
        ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         f"{user}@{engine_ip}", cmd], capture_output=True, text=True, timeout=120)


def ensure_transit_bridge(identifier):
    """Create vmbrW<id> on the host if absent (transit /30 between engine and pfSense WAN)."""
    br = f"vmbrW{identifier}"
    r = sh_host(f"ip link show {br} >/dev/null 2>&1 && echo exists || "
                f"(cat >/etc/network/interfaces.d/{br}.cfg <<EOF\nauto {br}\niface {br} inet manual\n"
                f"    bridge-ports none\n    bridge-stp off\n    bridge-fd 0\nEOF\n"
                f"ifup {br} 2>/dev/null || ifreload -a 2>/dev/null; echo created)")
    return br, (r.stdout or "").strip()


def clone_pfsense(identifier, template_vmid):
    """Full-clone the pfSense template to the per-team firewall VM with WAN+LAN NICs."""
    vmid = pf_vmid(identifier)
    existing = {v["vmid"] for v in range_ops.proxmox_api("GET", f"/nodes/{NODE}/qemu")["data"]}
    if vmid not in existing:
        upid = range_ops.proxmox_api("POST", f"/nodes/{NODE}/qemu/{template_vmid}/clone", data={
            "newid": vmid, "name": f"fw-team{identifier}", "full": 1,
            "description": f"tezcatlipoca-clone comp-pfsense-ad-2026-09-27"})["data"]
        range_ops.proxmox_api_wait(upid) if hasattr(range_ops, "proxmox_api_wait") else \
            range_ops.wait_for_proxmox_task(NODE, upid)
    # WAN = vtnet0 on transit, LAN = vtnet1 on the team bridge
    range_ops.proxmox_api("PUT", f"/nodes/{NODE}/qemu/{vmid}/config", data={
        "net0": f"virtio,bridge=vmbrW{identifier}",
        "net1": f"virtio,bridge=vmbr{identifier}"})
    return vmid


def inject_config(vmid, identifier):
    """Render the per-team config.xml and inject it into the clone's disk (offline)."""
    cfg = HERE / f"config-team{identifier}.xml"
    subprocess.run([sys.executable, str(HERE / "gen_pfsense_config.py"),
                    str(HERE / "pfsense-config-orig.xml"), str(identifier), str(cfg)], check=True)
    # push config to host + run the injection script (REQUIRES host-write permission)
    sh_host(f"mkdir -p /root/pf-inject")
    subprocess.run(["scp", "-i", KEY, "-o", "StrictHostKeyChecking=no", "-o",
                    "UserKnownHostsFile=/dev/null", str(cfg), str(HERE / "inject_pfsense.sh"),
                    f"root@{HOST}:/root/pf-inject/"], check=True, timeout=120)
    r = sh_host(f"bash /root/pf-inject/inject_pfsense.sh {vmid} /root/pf-inject/config-team{identifier}.xml")
    print(r.stdout, r.stderr)
    return r.returncode == 0


def wire_engine(engine_ip, identifier):
    """Add the engine's transit NIC address + route to the team subnet via pfSense, and
    drop the engine's old .1 on the team bridge. Idempotent."""
    # engine transit iface is the LAST added NIC; assume ensXX — resolve by the transit /30.
    script = (
        f"set -e; "
        # find the iface on the transit L2 (no IP yet) — match by having no address in 192.168.{identifier}
        f"TIF=$(ip -o link | awk -F': ' '{{print $2}}' | grep -E '^ens' | tail -1); "
        f"sudo ip addr add 172.31.{identifier}.1/30 dev $TIF 2>/dev/null || true; "
        f"sudo ip link set $TIF up; "
        f"sudo ip route replace 192.168.{identifier}.0/24 via 172.31.{identifier}.2; "
        # remove engine's own .1 on the team bridge so pfSense can own it
        f"TEAMIF=$(ip -o -4 addr show | awk '/192.168.{identifier}.1\\//{{print $2}}' | head -1); "
        f"[ -n \"$TEAMIF\" ] && sudo ip addr del 192.168.{identifier}.1/24 dev $TEAMIF || true; "
        f"echo wired-team{identifier}")
    return ssh_engine(engine_ip, script)


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: insert_inpath.py <engine_ip> <id1,id2,...>")
    engine_ip, ids = sys.argv[1], sys.argv[2].split(",")
    tmap = {v["name"]: v["vmid"] for v in range_ops.proxmox_api(
        "GET", "/cluster/resources?type=vm")["data"] if v.get("name")}
    tvid = tmap.get(PF_TEMPLATE_NAME)
    if not tvid:
        sys.exit(f"template {PF_TEMPLATE_NAME} not found/tagged — template it first")
    for identifier in ids:
        print(f"\n=== team {identifier} ===")
        print(ensure_transit_bridge(identifier))
        vmid = clone_pfsense(identifier, tvid)
        if not inject_config(vmid, identifier):
            sys.exit(f"config injection failed for team {identifier} (host-write permission?)")
        range_ops.proxmox_api("POST", f"/nodes/{NODE}/qemu/{vmid}/status/start")
        print(wire_engine(engine_ip, identifier).stdout)
    print("\nAll teams inserted. Verify: traceroute from a box to the engine crosses "
          "172.31.<id>.2; scoreboard shows AD boxes UP.")


if __name__ == "__main__":
    main()
