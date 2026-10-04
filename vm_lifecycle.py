"""VM power state and snapshot helpers (no ownership decisions in here)."""

from pve_api import proxmox_api, wait_for_proxmox_task
from utils import record_degradation

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
        # Never raises by design, but this is NOT a cosmetic warning: `tz-base`/`tz-ready`
        # are the rollback points, so a range whose snapshot failed cannot be rolled back
        # (`redeploy --mode rollback-base`) and a failed golden snapshot removes the
        # pre-plant guard. Live-found 2026-10-02: hdd filled during a Windows-heavy run and
        # both snapshots failed with `zfs error: ... out of space`, silently, mid-deploy.
        record_degradation(f"snapshot '{name}' failed",
                           f"vmid {vmid}: {str(e)[:200]}")
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
