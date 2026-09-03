"""Proxmox API layer, VM identity math, and (team, box) target abstraction.
Reads TF_VAR_proxmox_* from environment; no prompts or phase logic.
"""

import os
import time

import requests

# Proxmox's API token auth talks straight to the API over the same self-signed cert main.tf's
# provider block sets insecure=true for — same tradeoff, just from Python instead. Each entry
# point that imports this module also calls urllib3.disable_warnings().


### Proxmox API

def proxmox_api(method, path, **kwargs):
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    url = f"{endpoint}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
    # Retry transient connection blips (pveproxy drop) without masking real failures.
    last_exc = None
    for attempt in range(4):
        try:
            r = requests.request(method, url, headers=headers, verify=False, timeout=60, **kwargs)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def wait_for_proxmox_task(node, upid, timeout=1800):
    # Timeout must tolerate slow storage (clones/deletes can exceed 10 min on this host).
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Proxmox task {upid} timed out after {timeout}s")
        data = proxmox_api("GET", f"/nodes/{node}/tasks/{upid}/status")["data"]
        if data["status"] == "stopped":
            if data.get("exitstatus") != "OK":
                raise RuntimeError(f"Proxmox task {upid} failed: {data.get('exitstatus')}")
            return
        time.sleep(3)


def diagnose_unreachable_box(node, vmid):
    """Diagnose unreachable box via guest agent (virtio-serial, no network needed). Never raises."""

    try:
        ifaces = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )["data"]["result"]
        addrs = [
            a["ip-address"] for iface in ifaces for a in iface.get("ip-addresses", [])
            if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127.")
        ]
        iface_summary = f"has IPv4 {', '.join(addrs)}" if addrs else "has NO IPv4 address on any interface"
    except Exception as e:
        return f"      (guest agent unreachable for vmid {vmid}, can't diagnose further: {e})"

    cloud_init_summary = "(cloud-init status unavailable)"
    try:
        pid = proxmox_api(
            "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
            data={"command": ["cloud-init", "status", "--long"]},
        )["data"]["pid"]
        deadline = time.time() + 10
        while time.time() < deadline:
            s = proxmox_api(
                "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
                params={"pid": pid},
            )["data"]
            if s.get("exited"):
                out = (s.get("out-data") or "").strip()
                cloud_init_summary = out.splitlines()[0] if out else "(no output)"
                break
            time.sleep(0.5)
    except Exception:
        pass  # keep the default "(cloud-init status unavailable)" — non-fatal either way

    return f"      guest agent (vmid {vmid}): {iface_summary}; cloud-init {cloud_init_summary}"


def guest_agent_exec_root(node, vmid, script, timeout=60):
    """Run bash as root via QEMU guest agent (virtio-serial; no sudo needed, bypasses broken sudo).
    Returns (exit_code, stdout, stderr); raises on agent failure.
    """
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": ["bash", "-c", script]},
    )["data"]["pid"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        time.sleep(1)
    raise RuntimeError(f"guest-agent exec on vmid {vmid} didn't finish within {timeout}s")


def guest_agent_exec_windows(node, vmid, ps_script, timeout=120):
    """Run PowerShell as SYSTEM via guest agent (-EncodedCommand avoids quoting issues).
    Only channel before Windows bootstrap. Returns (exit_code, stdout, stderr).
    """
    import base64
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
        ]},
    )["data"]["pid"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        time.sleep(1)
    raise RuntimeError(f"guest-agent exec on vmid {vmid} didn't finish within {timeout}s")


def wait_for_guest_agent(node, vmid, timeout=300):
    """Wait for guest agent ping (virtio-serial, no network needed). Never raises."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping", data={})
            return True
        except Exception:
            pass
        time.sleep(5)
    return False


### VM identity

# VM IDs are 200 + identifier*10 + box_index (mirrored in main.tf's team_box.vm_id), which
# leaves each team a stride of exactly 10. An 11th box would land on the next team's first box
# and silently clobber it, so the scheme caps the box count rather than the picker.
MAX_BOXES_PER_TEAM = 10

# Team identifiers are `100 + i` and become the subnet's third octet (192.168.<identifier>.x),
# which must stay a valid, non-zero octet (1-254) — i.e. i <= 154. Past that, identifiers like
# 256 would produce an invalid IP and silently break the whole range.
MAX_TEAMS = 154

# The scoring engine's vmid is fixed in main.tf, not derived from the formula above.
SCORING_ENGINE_VMID = 1000


def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


### VM lifecycle

def vm_status(node, vmid):
    """Current status string ('running'/'stopped'/...) or None if the VM doesn't exist."""
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    return vm.get("status") if vm else None


def stop_vm(node, vmid):
    # Graceful ACPI first for filesystem consistency; force stop if ignored.
    try:
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)
    except RuntimeError:
        print(f"    vmid {vmid} ignored ACPI shutdown — forcing stop")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)


def start_vm(node, vmid, timeout=120):
    """Start the VM if it isn't already running. No-op for a VM that doesn't exist."""
    status = vm_status(node, vmid)
    if status is None or status == "running":
        return
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)


def destroy_vm_if_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        return
    if vm.get("status") == "running":
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid)
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}")["data"]
    wait_for_proxmox_task(node, upid)


### Targets — the (team, box) pair every per-box operation actually works on

def enumerate_targets(teams, boxes):
    """One target per (team, box) with correct vmid/IP/name derivations.
    Build from full lists then filter; never filter boxes and re-enumerate (vmid is positional).
    """
    return [
        {
            "team_key": team_key,
            "identifier": str(team["identifier"]),
            "box": box,
            "box_name": box["name"],
            "box_idx": box_idx,
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "vmid": vm_id_for(team["identifier"], box_idx),
            # Proxmox names team1's boxes "team1-<box>" (main.tf's local.team_vms) but the
            # API-created clones "<identifier>-<box>" (clone_team_boxes). Record what this
            # target's VM is actually called so nothing has to re-derive it.
            "vm_name": (f"{team_key}-{box['name']}" if team_key == "team1"
                        else f"{team['identifier']}-{box['name']}"),
            # nakon's machine list uses the opposite order — see generate_nakon_config().
            "machine": f"{box['name']}-team{team['identifier']}",
        }
        for team_key, team in teams.items()
        for box_idx, box in enumerate(boxes)
    ]


def describe_target(t):
    return f"{t['team_key']}/{t['box_name']}  vmid {t['vmid']}  {t['ip']}"


### Snapshots
# tz-base: booted, networked, pre-nakon. tz-ready: as-delivered post-nakon+hardening.
# Note: team2+ tz-base is cloned after phase 5, so it carries team1's configs but is still
# the per-box "before nakon" point for that team.
SNAP_BASE = "tz-base"
SNAP_READY = "tz-ready"


def list_snapshots(node, vmid):
    """Snapshot names on this VM, excluding Proxmox's synthetic `current` entry.

    Returns an empty set if the VM is gone or the API refuses — callers treat "no snapshot" and
    "couldn't ask" the same way (fall back to a mode that doesn't need one).
    """
    try:
        data = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/snapshot")["data"]
    except Exception:
        return set()
    return {s["name"] for s in data if s.get("name") != "current"}


def delete_snapshot(node, vmid, name, timeout=600):
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)


def take_snapshot(node, vmid, name, description="", timeout=900):
    """Take disk-only snapshot (vmstate 0; fsfreeze via guest agent). Never raises;
    snapshots are optional recovery (thick LVM can't snapshot). Replaces existing name.
    """
    try:
        if name in list_snapshots(node, vmid):
            delete_snapshot(node, vmid, name)
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/snapshot", data={
            "snapname": name,
            "vmstate": 0,
            "description": description or f"tezcatlipoca {name}",
        })["data"]
        wait_for_proxmox_task(node, upid, timeout=timeout)
        return True
    except Exception as e:
        print(f"    WARNING: snapshot '{name}' failed for vmid {vmid}: {e}")
        return False


def rollback_snapshot(node, vmid, name, timeout=900, restart=True):
    """Rollback to snapshot (requires stop/start for no-RAM snapshot). Raises on failure."""

    if vm_status(node, vmid) == "running":
        stop_vm(node, vmid)
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)
    if restart:
        start_vm(node, vmid)


def snapshot_support_hint(node, vmid):
    """One-line explanation for why snapshots may be unavailable, for error messages."""
    return (
        f"vmid {vmid} on node {node} has no tezcatlipoca snapshots. Either this range was "
        f"deployed before snapshotting was added, or TF_VAR_datastore doesn't support "
        f"snapshots (thick LVM can't; qcow2 on file storage, ZFS, LVM-thin and Ceph can). "
        f"Use --mode reconfigure (re-runs configuration on the live box) or --mode rebuild "
        f"(recreates the VM from its template)."
    )
