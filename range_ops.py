"""Proxmox API layer, VM identity math, and (team, box) target abstraction."""

import json
import os
import shutil
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

from constants import MAX_BOXES_PER_TEAM, MAX_TEAMS, SCORING_ENGINE_VMID, SNAP_BASE, SNAP_READY

REPO_ROOT = Path(__file__).resolve().parent
_TF_TEMPLATE_DIR = REPO_ROOT / "terraform"
# Only the .tf sources + provider lock are per-competition template files; the
# state, .terraform plugin dir, and terraform.tfvars.json live locally per comp.
_TF_TEMPLATE_FILES = ("main.tf", "variables.tf", "outputs.tf", ".terraform.lock.hcl")


def terraform_dir(comp_dir):
    """Per-competition Terraform working dir (its own state + lock), so two
    competitions can `terraform apply` concurrently on one node instead of
    contending on the single shared terraform/terraform.tfstate."""
    return Path(comp_dir) / "terraform"


def ensure_terraform_workdir(comp_dir):
    """Materialize competitions/<id>/terraform/ from the canonical terraform/
    template: (re)copy the .tf sources + provider lock, never touching the local
    tfstate/.terraform/terraform.tfvars.json. Idempotent."""
    dst = terraform_dir(comp_dir)
    dst.mkdir(parents=True, exist_ok=True)
    for name in _TF_TEMPLATE_FILES:
        src = _TF_TEMPLATE_DIR / name
        if src.exists():
            shutil.copy2(src, dst / name)
    return dst


def terraform_plugin_cache_dir():
    """Shared provider-plugin cache so each per-comp `terraform init` links the
    provider from disk instead of re-downloading it."""
    cache = _TF_TEMPLATE_DIR / ".terraform-plugin-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


class _FingerprintAdapter(HTTPAdapter):
    def __init__(self, fingerprint, **kw):
        self._fingerprint = fingerprint
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block,
                                       assert_fingerprint=self._fingerprint, **kw)


def proxmox_request(method, url, **kwargs):
    """requests wrapper honoring optional TLS pinning. PROXMOX_TLS_FINGERPRINT (sha256) pins the
    cert; PROXMOX_CA_BUNDLE verifies against a CA path; neither set => verify=False (lab default)."""
    fingerprint = os.environ.get("PROXMOX_TLS_FINGERPRINT")
    ca = os.environ.get("PROXMOX_CA_BUNDLE")
    session = requests.Session()
    if fingerprint:
        session.mount("https://", _FingerprintAdapter(fingerprint))
        kwargs.setdefault("verify", False)
    else:
        kwargs.setdefault("verify", ca or False)
    return session.request(method, url, **kwargs)


def proxmox_api(method, path, **kwargs):
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    url = f"{endpoint}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
    last_exc = None
    for attempt in range(4):
        try:
            r = proxmox_request(method, url, headers=headers, timeout=60, **kwargs)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def wait_for_proxmox_task(node, upid, timeout=1800):
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
        pass

    return f"      guest agent (vmid {vmid}): {iface_summary}; cloud-init {cloud_init_summary}"


def guest_agent_exec_root(node, vmid, script, timeout=60):
    """Run bash as root via the QEMU guest agent; returns (exit_code, stdout, stderr)."""
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
    """Run PowerShell as SYSTEM via the guest agent; only channel before Windows bootstrap."""
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




def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


def box_index(boxes, box_name):
    """Canonical position of a box by name — matches terraform's index(names, name)."""
    for i, b in enumerate(boxes):
        if b["name"] == box_name:
            return i
    raise KeyError(f"box {box_name!r} not in boxes list")


def persist_targets(comp_dir, targets, boxes):
    """Freeze (team,box)->vmid at deploy so later tools don't recompute it from boxes.json order."""
    data = {
        "box_order": [b["name"] for b in boxes],
        "targets": {
            t["vm_name"]: {
                "team_key": t["team_key"],
                "identifier": t["identifier"],
                "box_name": t["box_name"],
                "vmid": t["vmid"],
                "ip": t["ip"],
            }
            for t in targets
        },
    }
    (Path(comp_dir) / "targets.json").write_text(json.dumps(data, indent=2))


def load_targets(comp_dir, teams, boxes):
    """Targets with vmid read from targets.json (frozen at deploy); recompute if absent.

    Refuses when boxes.json was reordered since deploy: vmid is positional, so a
    reorder would silently retarget a different VM (and terraform would diverge too)."""
    path = Path(comp_dir) / "targets.json"
    if not path.exists():
        print("  (targets.json absent — deriving vmids from boxes.json order; "
              "reorder-unsafe for ranges deployed before this was added)")
        return enumerate_targets(teams, boxes)
    data = json.loads(path.read_text())
    current = [b["name"] for b in boxes]
    if data.get("box_order") != current:
        raise SystemExit(
            f"  ERROR: boxes.json box order changed since deploy (was {data.get('box_order')}, "
            f"now {current}). VM identity (vmid) is positional — restore the original order in "
            "boxes.json before redeploy/verify, or tear down and redeploy from scratch."
        )
    frozen = data.get("targets", {})
    result = []
    for t in enumerate_targets(teams, boxes):
        f = frozen.get(t["vm_name"])
        if f is not None:
            t = {**t, "vmid": f["vmid"], "ip": f.get("ip", t["ip"])}
        result.append(t)
    return result



def vm_status(node, vmid):
    """Current status string ('running'/'stopped'/...) or None if the VM doesn't exist."""
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    return vm.get("status") if vm else None


def stop_vm(node, vmid):
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



def enumerate_targets(teams, boxes):
    """One target per (team, box); build from full lists then filter — vmid is positional."""
    return [
        {
            "team_key": team_key,
            "identifier": str(team["identifier"]),
            "box": box,
            "box_name": box["name"],
            "box_idx": box_idx,
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "vmid": vm_id_for(team["identifier"], box_idx),
            "vm_name": (f"{team_key}-{box['name']}" if team_key == "team1"
                        else f"{team['identifier']}-{box['name']}"),
            "machine": f"{box['name']}-team{team['identifier']}",
        }
        for team_key, team in teams.items()
        for box_idx, box in enumerate(boxes)
    ]


def describe_target(t):
    return f"{t['team_key']}/{t['box_name']}  vmid {t['vmid']}  {t['ip']}"




def list_snapshots(node, vmid):
    """Snapshot names on this VM, excluding Proxmox's synthetic `current`; empty set when unanswerable."""
    try:
        data = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/snapshot")["data"]
    except Exception:
        return set()
    return {s["name"] for s in data if s.get("name") != "current"}


def delete_snapshot(node, vmid, name, timeout=600):
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}")["data"]
    wait_for_proxmox_task(node, upid, timeout=timeout)


def take_snapshot(node, vmid, name, description="", timeout=900):
    """Take a disk-only snapshot, replacing any existing one; never raises."""
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
