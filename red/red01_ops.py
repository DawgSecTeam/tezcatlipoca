"""red01: the red team's VM, its network identity, engine rules and teardown.

Red is a range component like the engine and the team boxes: this module clones it
from the red template, pins its network, tags it for ownership, writes the engine's
red-segment firewall, and destroys exactly what the run deployed. No sibling
checkout, no external teardown vocabulary.
"""

import base64
import subprocess
from pathlib import Path

from common.constants import (GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM, RED_SEGMENT_GW,
                              RED_SEGMENT_IP, RED_SEGMENT_SUBNET)
from common.utils import compfile_value
from estate import config
from infra.client import proxmox_api, wait_for_proxmox_task
from infra.guest_exec import guest_agent_exec_root, wait_for_guest_agent
from infra.lifecycle import start_vm
from infra.ownership import clone_marker, destroy_vm_if_exists

RED_USER = "sysadmin"
NAME = "tlaloc-red01"
DEFAULT_TEMPLATE = "base-ubuntu24.04-fix"
# What the agent needs on the box: its config reader and the recon scan. Mythic's own
# dependencies are Gaia's job.
DEPS = "python3-yaml cloud-guest-utils lvm2 nmap"
ROUTE_NOTE = "192.168.0.0/16"


# --------------------------------------------------------------------------- #
# identity: reserved once per competition, never a shared default
# --------------------------------------------------------------------------- #

def default_red_vmid(engine_vmid, slots_used):
    """One golden stride past this placement's last golden band."""
    return int(engine_vmid) + GOLDEN_VMID_OFFSET + (int(slots_used) + 1) * MAX_BOXES_PER_TEAM


def red01_vmid_override(comp_dir):
    raw = compfile_value(Path(comp_dir) / "Compfile", "red01_vmid", "")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def red01_vmid_for(comp_dir, engine_vmid=None, placement=None):
    """Override, recorded placement, else the stride — the placement wins once written."""
    override = red01_vmid_override(comp_dir)
    if override is not None:
        return override
    recorded = ((placement or {}).get("red01") or {}).get("vmid")
    if recorded:
        return int(recorded)
    if engine_vmid is None:
        return None
    return default_red_vmid(engine_vmid, len((placement or {}).get("satellites") or []))


def red01_ip_override(comp_dir):
    """The operator's Compfile `red01_ip`, else ''."""
    return compfile_value(Path(comp_dir) / "Compfile", "red01_ip", "") or ""


def red01_ip_for(comp_dir, placement=None):
    """The operator's address, the placement's recorded one, else '' (select at deploy)."""
    return red01_ip_override(comp_dir) or \
        (((placement or {}).get("red01") or {}).get("ip") or "")


def red01_ip_is_explicit(comp_dir):
    return bool(red01_ip_override(comp_dir))


def red01_target(comp_dir, engine_vmid=None, placement=None):
    return red01_vmid_for(comp_dir, engine_vmid, placement), red01_ip_for(comp_dir, placement)


def identity(comp_dir, engine_vmid, node, placement=None):
    """This run's red01 slot, before anything is created."""
    vmid, ip = red01_target(comp_dir, engine_vmid, placement)
    return {"vmid": vmid, "ip": ip, "node": node,
            "explicit_ip": red01_ip_is_explicit(comp_dir)}


def select_red_ip(comp_dir, placement, engine_mgmt_ip="", comp_tags=None):
    """Pick and record an address from the red band immediately before the clone."""
    from infra import collisions
    from multinode.placement_record import write_red01_ip
    claimed = {engine_mgmt_ip or "", config.active().engine.mgmt_gw or ""}
    nodes = [config.active().proxmox.node]
    skip = set()
    if placement:
        claimed |= {s["jump_mgmt_ip"] for s in placement.get("satellites") or []}
        nodes = [rec["node"] for rec in placement["nodes"].values()]
        skip |= {s["jump_vmid"] for s in placement.get("satellites") or []}
        skip.add(placement.get("engine_vmid") or 0)
    ip, unchecked = collisions.select_red_ip(
        collisions.scan_vms_for(nodes), claimed, skip, comp_tags)
    if ip and placement:
        write_red01_ip(comp_dir, placement, ip)
    return ip, unchecked


def recorded_identity(comp_dir):
    """What a previous deploy of this competition recorded: {vmid, ip, node} or {}."""
    from estate import state
    document = state.EstateState.load(comp_dir)
    recorded = document.section("red") or {}
    placed = (document.data.get("placement") or {}).get("red01") or {}
    return {"vmid": recorded.get("vmid") or placed.get("vmid"),
            "ip": recorded.get("ip") or placed.get("ip") or "",
            "node": recorded.get("node") or placed.get("node")}


def record_identity(comp_dir, placement, *, vmid, ip, node):
    """Freeze the slot this run created into the state (and the placement, when there is one)."""
    from estate import state
    from multinode.placement_record import write_placement
    # A single-node deploy has no placement document; the state's own `red` section is
    # what teardown reads, so it must be written either way — otherwise a range that
    # dies after the clone leaves a tagged red01 no teardown can claim.
    document = state.EstateState.load(comp_dir)
    document.section("red").update(vmid=int(vmid), ip=ip, node=node)
    document.save()
    if placement is None:
        return
    placement["red01"] = {"vmid": int(vmid), "ip": ip, "node": node,
                          "explicit_ip": red01_ip_is_explicit(comp_dir)}
    write_placement(comp_dir, placement)


# --------------------------------------------------------------------------- #
# the VM
# --------------------------------------------------------------------------- #

def ssh_argv(ssh_key, host, remote, timeout=30, user=RED_USER):
    return ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={timeout}", f"{user}@{host}", remote]


def _run(cmd, *, runner=None, timeout=60, input_bytes=None):
    runner = runner or _default_runner
    return runner(cmd, input=input_bytes, timeout=timeout)


def _default_runner(cmd, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def _config(node, vmid, data):
    """PUT a clone's config; retry the brief post-clone publish window."""
    import time
    for attempt in range(4):
        try:
            proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data=data)
            return
        except Exception as exc:                                # noqa: BLE001 - retried
            if attempt == 3:
                raise RuntimeError(f"red01 config PUT failed on vmid {vmid}: {exc}") from exc
            time.sleep(2 * (attempt + 1))


def _exit_codes(*names):
    return f"echo {' '.join(names)}"


def find_template(node, name):
    for vm in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]:
        if vm.get("template") and vm.get("name") == name:
            return int(vm["vmid"])
    raise RuntimeError(f"red01 template {name!r} not found on node {node} — install it "
                       f"(a qemu-guest-agent + cloud-init Ubuntu template)")


def _netplan(iface, ip, gw, red_iface):
    # The pinned static config replaces cloud-init's (which carried the resolver), so DNS has
    # to be named here or the box can reach 8.8.8.8 and still resolve nothing.
    return (f"network:\n  version: 2\n  ethernets:\n"
            f"    {iface}:\n      dhcp4: no\n      addresses: ['{ip}/24']\n"
            f"      nameservers:\n        addresses: [8.8.8.8]\n"
            f"      routes:\n        - to: default\n          via: {gw}\n"
            f"    {red_iface}:\n      dhcp4: no\n      addresses: ['{RED_SEGMENT_IP}/24']\n"
            f"      routes:\n        - to: {ROUTE_NOTE}\n          via: {RED_SEGMENT_GW}\n")


def _exec(node, vmid, script, timeout=300):
    rc, out, err = guest_agent_exec_root(node, vmid, script, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"guest command failed on red01 (rc {rc}): "
                           f"{(err or out or '')[:300]}")
    return out or ""


def deploy(comp_dir, *, engine_vmid, ssh_key, ownership, placement=None, node=None,
           engine_ip="", red_bridge=None, template=None, runner=None, dry_run=False):
    """Clone/configure red01 on this run's reserved slot and return its identity.

    Idempotent for a resume: a VM already carrying this run's tags is adopted and
    reconfigured, never destroyed.
    """
    comp_dir = Path(comp_dir)
    node = node or config.active().proxmox.node
    vmid = red01_vmid_for(comp_dir, engine_vmid, placement)
    if vmid is None:
        raise RuntimeError("cannot reserve red01's vmid: no Compfile red01_vmid, no "
                           "recorded placement, and no scoring vmid to derive from")
    ip = red01_ip_for(comp_dir, placement)
    if not ip:
        ip, unchecked = select_red_ip(comp_dir, placement, engine_ip,
                                     comp_tags=ownership.tags)
        if not ip:
            raise RuntimeError(f"no free address in the red band for red01 "
                               f"(uncheckable: {unchecked})")
    template = template or DEFAULT_TEMPLATE
    pub_path = Path(str(ssh_key) + ".pub")
    pub_key = pub_path.read_text(encoding="utf-8").strip() if pub_path.is_file() else ""
    if not pub_key:
        raise RuntimeError(f"SSH public key {pub_path} is missing or empty — red01 "
                           f"cannot be cloned without the key that reaches it")
    gw = config.active().engine.mgmt_gw or "10.0.0.1"
    if dry_run:
        return {"ok": True, "vmid": vmid, "ip": ip, "node": node, "dry_run": True}

    # 1. Clone (or adopt a resume) and stamp ownership. The clone POST carries the
    #    competition's marker; the full tag set lands with the config PUT right after,
    #    so a crash between the two still leaves a VM this competition's teardown claims.
    existing = _live(node, vmid, ownership)
    if existing is None:
        tpl = find_template(node, template)
        print(f"  red01      : cloning vmid {vmid} from template {template} (vmid {tpl})")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{tpl}/clone",
                           data={"newid": vmid, "name": NAME, "full": 0,
                                 "description": clone_marker(ownership.comp)})["data"]
        wait_for_proxmox_task(node, upid)
    else:
        print(f"  red01      : adopting vmid {vmid} (this run's) for reconfigure")
    from golden.golden_target_ops import _quote_sshkeys
    data = {"name": NAME, "onboot": 1, "agent": 1,
            "net0": f"virtio,bridge={_mgmt_bridge(placement)}",
            "ipconfig0": f"ip={ip}/24,gw={gw}",
            "ciuser": RED_USER, "sshkeys": _quote_sshkeys(pub_key),
            "tags": ";".join(sorted(ownership.tags)),
            "description": f"tlaloc red01 comp={ownership.comp} id={ownership.run_id}"}
    if red_bridge:
        data["net1"] = f"virtio,bridge={red_bridge}"
    _config(node, vmid, data)
    start_vm(node, vmid)
    if not wait_for_guest_agent(node, vmid, timeout=420):
        raise RuntimeError(f"red01 vmid {vmid} started but its guest agent never answered")
    # cloud-init can restart qemu-guest-agent while it installs upgrades, which drops the exec
    # pid underneath this wait. The wait is idempotent, so re-attach and ask it again.
    for attempt in range(3):
        try:
            _exec(node, vmid, "timeout 300 cloud-init status --wait >/dev/null 2>&1; true", 330)
            break
        except RuntimeError as e:
            if "lost exec pid" not in str(e) or attempt == 2:
                raise
            wait_for_guest_agent(node, vmid, timeout=300)

    # 2. Pin the network ourselves: cloud-init is disabled on this box because a
    #    kernel-upgrade reboot has come back with no IPv4 at all when it re-applied.
    ifaces = _exec(node, vmid, "ls /sys/class/net/ | grep -v lo").split()
    iface = _exec(node, vmid,
                  "ip -4 route show default | awk '{print $5; exit}'").strip()
    iface = iface if iface in ifaces else (ifaces[0] if ifaces else None)
    if not iface:
        raise RuntimeError("red01 reports no network interface")
    red_iface = next((i for i in ifaces if i != iface), None)
    if not red_iface:
        raise RuntimeError("red01 has one NIC but the red bridge needs a second — "
                           "the clone did not get net1 on the red segment")
    netplan = _netplan(iface, ip, gw, red_iface)
    _write(node, vmid, "/etc/netplan/51-tlaloc.yaml", netplan, "600")
    _write(node, vmid, "/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg",
           "network: {config: disabled}\n", "644")
    _exec(node, vmid, "rm -f /etc/netplan/50-cloud-init.yaml && netplan apply && "
                      f"ip -4 -brief addr show | grep -qF '{ip}'", 90)

    # 3. The agent's apt dependencies (Mythic's are installed by Gaia).
    _exec(node, vmid, "export DEBIAN_FRONTEND=noninteractive; "
                      "apt-get update -qq && apt-get install -y -qq " + DEPS, 900)
    return {"ok": True, "vmid": vmid, "ip": ip, "node": node,
            "segment_ip": RED_SEGMENT_IP, "segment_subnet": RED_SEGMENT_SUBNET}


def _live(node, vmid, ownership):
    """True when vmid is already a VM this run owns; refuse an occupied foreign slot."""
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        return None
    from infra.ownership import OWNED, ownership_verdict
    tags = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"].get("tags")
    verdict, _ = ownership_verdict(node, vmid, tags, ownership.tags)
    if verdict == OWNED:
        return vm
    raise RuntimeError(
        f"red01 vmid {vmid} is occupied by {vm.get('name') or '?'} and is not this run's "
        f"(tags {tags!r}) — a deploy never reconfigures or destroys another run's range. "
        f"Set red01_vmid in the Compfile, or reclaim the range with destroy-competition.py")


def _mgmt_bridge(placement):
    from multinode.placement_record import resolved_mgmt_bridge
    return resolved_mgmt_bridge(placement)


def _write(node, vmid, path, content, mode):
    blob = base64.b64encode(content.encode()).decode()
    _exec(node, vmid, f"echo '{blob}' | base64 -d > {path} && chmod {mode} {path}", 60)


# --------------------------------------------------------------------------- #
# the engine side: the red segment's rules and the C2 callback
# --------------------------------------------------------------------------- #

def dnat_destinations(team_identifiers, callback_host=""):
    """The addresses boxes dial for C2: their own gateway, plus the transit address
    when an in-path firewall makes the engine reachable that way."""
    dsts = [f"192.168.{identifier}.1" for identifier in team_identifiers]
    if callback_host and callback_host not in dsts:
        dsts.append(callback_host)
    return dsts


def firewall_script(*, team_identifiers, callback_port, callback_host="",
                    red_ip=RED_SEGMENT_IP, red_subnet=RED_SEGMENT_SUBNET):
    """The re-assert script for the engine: red-segment forwarding plus the C2 DNAT.

    Rule order is load-bearing: return traffic must be accepted before the
    DROP-toward-red, and the DNAT must sit before the team-SNAT exemption, or the
    agent sees every check-in as having come from the engine.
    """
    lines = [
        "#!/bin/bash",
        "# red segment: red keeps its source address end-to-end, blue can firewall it,",
        "# and scoring still sources from the team gateway. Re-asserted by a 30s timer",
        "# because Docker on the engine wipes iptables when its containers restart.",
        f"iptables -C FORWARD -s {red_subnet} -d 192.168.0.0/16 -j ACCEPT 2>/dev/null || "
        f"iptables -I FORWARD 1 -s {red_subnet} -d 192.168.0.0/16 -j ACCEPT",
        f"iptables -C FORWARD -d {red_subnet} -m conntrack --ctstate RELATED,ESTABLISHED "
        f"-j ACCEPT 2>/dev/null || iptables -I FORWARD 2 -d {red_subnet} "
        f"-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
        f"iptables -C FORWARD -d {red_subnet} -j DROP 2>/dev/null || "
        f"iptables -I FORWARD 3 -d {red_subnet} -j DROP",
    ]
    if callback_port:
        port = int(callback_port)
        for dst in dnat_destinations(team_identifiers, callback_host):
            for proto in ("tcp", "udp"):
                lines += [
                    f"iptables -t nat -C PREROUTING -p {proto} -d {dst} --dport {port} "
                    f"-j DNAT --to-destination {red_ip}:{port} 2>/dev/null || "
                    f"iptables -t nat -A PREROUTING -p {proto} -d {dst} --dport {port} "
                    f"-j DNAT --to-destination {red_ip}:{port}",
                    f"iptables -C FORWARD -p {proto} -d {red_ip} --dport {port} -j ACCEPT "
                    f"2>/dev/null || iptables -I FORWARD 1 -p {proto} -d {red_ip} "
                    f"--dport {port} -j ACCEPT",
                    f"iptables -t nat -C POSTROUTING -s 192.168.0.0/16 -d {red_ip} "
                    f"-p {proto} --dport {port} -j ACCEPT 2>/dev/null || "
                    f"iptables -t nat -I POSTROUTING 1 -s 192.168.0.0/16 -d {red_ip} "
                    f"-p {proto} --dport {port} -j ACCEPT",
                ]
    return "\n".join(lines) + "\n"


_RED_SERVICE = ("[Unit]\nDescription=red segment rules (red01)\nAfter=docker.service\n\n"
                "[Service]\nType=oneshot\nExecStart=/usr/local/sbin/range-red-firewall.sh\n")
_RED_TIMER = ("[Unit]\nDescription=Re-assert the red segment rules\n\n"
              "[Timer]\nOnBootSec=35\nOnUnitActiveSec=30\n\n[Install]\nWantedBy=timers.target\n")


def install_engine_rules(engine_ip, ssh_key, *, engine_user, team_identifiers,
                         callback_port, callback_host="", runner=None, timeout=180):
    """Install the red rules + their timer on the scoring engine."""
    script = firewall_script(team_identifiers=team_identifiers,
                             callback_port=callback_port, callback_host=callback_host)
    units = {
        "/usr/local/sbin/range-red-firewall.sh": (script, "755"),
        "/etc/systemd/system/range-red-firewall.service": (_RED_SERVICE, "644"),
        "/etc/systemd/system/range-red-firewall.timer": (_RED_TIMER, "644"),
    }
    for path, (content, mode) in units.items():
        blob = base64.b64encode(content.encode()).decode()
        proc = _run(ssh_argv(ssh_key, engine_ip,
                             f"echo '{blob}' | sudo base64 -d | sudo tee {path} "
                             f">/dev/null && sudo chmod {mode} {path}",
                             timeout=timeout, user=engine_user), runner=runner)
        if getattr(proc, "returncode", 1) != 0:
            raise RuntimeError(f"installing {path} on the engine failed: "
                               f"{_tail(proc)}")
    proc = _run(ssh_argv(ssh_key, engine_ip,
                         "sudo systemctl daemon-reload && sudo systemctl enable "
                         "--now range-red-firewall.timer", timeout=timeout,
                         user=engine_user), runner=runner)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"enabling the red firewall timer failed: {_tail(proc)}")
    return {"ok": True}


def remove_engine_rules(engine_ip, ssh_key, *, engine_user, runner=None, timeout=120,
                        team_identifiers=(), callback_port=None, callback_host=""):
    """Best effort: stop the re-assert and remove the rules and unit (the engine survives
    a teams-only teardown, so the red segment must not outlive its red01)."""
    dnat = ""
    if callback_port:
        for dst in dnat_destinations(team_identifiers, callback_host):
            for proto in ("tcp", "udp"):
                dnat += (f"while sudo iptables -t nat -D PREROUTING -p {proto} -d {dst} "
                         f"--dport {int(callback_port)} -j DNAT --to-destination "
                         f"{RED_SEGMENT_IP}:{int(callback_port)} 2>/dev/null; do :; done; "
                         f"while sudo iptables -D FORWARD -p {proto} -d {RED_SEGMENT_IP} "
                         f"--dport {int(callback_port)} -j ACCEPT 2>/dev/null; do :; done; "
                         f"while sudo iptables -t nat -D POSTROUTING -s 192.168.0.0/16 -d "
                         f"{RED_SEGMENT_IP} -p {proto} --dport {int(callback_port)} -j ACCEPT "
                         f"2>/dev/null; do :; done; ")
    proc = _run(ssh_argv(ssh_key, engine_ip, (
        "sudo systemctl disable --now range-red-firewall.timer 2>/dev/null; "
        "sudo rm -f /usr/local/sbin/range-red-firewall.sh "
        "/etc/systemd/system/range-red-firewall.service "
        "/etc/systemd/system/range-red-firewall.timer; "
        "sudo systemctl daemon-reload 2>/dev/null; "
        f"while sudo iptables -D FORWARD -s {RED_SEGMENT_SUBNET} -d 192.168.0.0/16 "
        "-j ACCEPT 2>/dev/null; do :; done; "
        f"while sudo iptables -D FORWARD -d {RED_SEGMENT_SUBNET} "
        "-m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; do :; done; "
        f"while sudo iptables -D FORWARD -d {RED_SEGMENT_SUBNET} -j DROP 2>/dev/null; do :; done; "
        + dnat),
        timeout=timeout, user=engine_user), runner=runner)
    return {"ok": getattr(proc, "returncode", 1) == 0, "detail": _tail(proc)}


def _tail(proc, n=300):
    return (((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or ""))[-n:])


# --------------------------------------------------------------------------- #
# teardown
# --------------------------------------------------------------------------- #

def destroy(comp_dir, *, ownership, node=None, vmid=None, engine_ip=None, ssh_key=None,
            engine_user=None, runner=None, team_identifiers=(), callback_port=None,
            callback_host=""):
    """Remove this run's red01 and the engine rules it installed.

    The VM is destroyed through the ownership guard, so a recycled vmid or another
    run's red01 is refused rather than deleted.
    """
    recorded = recorded_identity(comp_dir)
    node = node or recorded.get("node") or config.active().proxmox.node
    vmid = vmid or recorded.get("vmid")
    removed = {"vm": False, "rules": False}
    if engine_ip and ssh_key and engine_user:
        removed["rules"] = remove_engine_rules(
            engine_ip, ssh_key, engine_user=engine_user, runner=runner,
            team_identifiers=team_identifiers, callback_port=callback_port,
            callback_host=callback_host).get("ok", False)
    if not vmid:
        raise RuntimeError("red01's vmid is not recorded for this competition — cannot "
                           "prove what to destroy")
    destroy_vm_if_exists(node, int(vmid), expect_tags=ownership.tags)
    removed["vm"] = True
    from estate import state
    document = state.EstateState.load(comp_dir)
    (document.data.get("placement") or {}).pop("red01", None)
    document.save()
    return removed


def name_for(comp_name, run_id):
    return f"tlaloc-red01 comp={comp_name} id={run_id}"
