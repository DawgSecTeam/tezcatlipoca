"""VM power state and snapshot helpers (no ownership decisions in here)."""

from infra.client import proxmox_api, run_vm_task
from common.utils import record_degradation

# PVE's own ACPI-shutdown wait, passed as the `timeout` of POST /status/shutdown. It must
# expire inside OUR client wait: giving up on a still-running `qmshutdown` task and
# force-stopping collides with the per-VM lock that task still holds (crimson-relay phase 4,
# run-dd88db6d, 2026-10-16). With it, PVE ends the task on its own and releases the lock.
SHUTDOWN_GRACE_S = 120

def vm_status(node, vmid):
    """Current status string ('running'/'stopped'/...) or None if the VM doesn't exist."""
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    return vm.get("status") if vm else None


def stop_vm(node, vmid):
    try:
        run_vm_task(node, vmid, "POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown",
                    data={"timeout": SHUTDOWN_GRACE_S}, timeout=SHUTDOWN_GRACE_S + 120)
    except RuntimeError:
        pass
    if vm_status(node, vmid) == "running":
        print(f"    vmid {vmid} ignored ACPI shutdown — forcing stop")
        run_vm_task(node, vmid, "POST", f"/nodes/{node}/qemu/{vmid}/status/stop", timeout=120)


def start_vm(node, vmid, timeout=120):
    """Start the VM if it isn't already running. No-op for a VM that doesn't exist."""
    status = vm_status(node, vmid)
    if status is None or status == "running":
        return
    run_vm_task(node, vmid, "POST", f"/nodes/{node}/qemu/{vmid}/status/start", timeout=timeout)


def list_snapshots(node, vmid):
    """Snapshot names on this VM, excluding Proxmox's synthetic `current`; empty set when unanswerable."""
    try:
        data = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/snapshot")["data"]
    except Exception:
        return set()
    return {s["name"] for s in data if s.get("name") != "current"}


def delete_snapshot(node, vmid, name, timeout=600):
    run_vm_task(node, vmid, "DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}",
                timeout=timeout)


def take_snapshot(node, vmid, name, description="", timeout=900):
    """Take a disk-only snapshot, replacing any existing one; never raises."""
    try:
        if name in list_snapshots(node, vmid):
            delete_snapshot(node, vmid, name)
        run_vm_task(node, vmid, "POST", f"/nodes/{node}/qemu/{vmid}/snapshot", data={
            "snapname": name,
            "vmstate": 0,
            "description": description or f"tezcatlipoca {name}",
        }, timeout=timeout)
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
    """Rollback to snapshot (requires stop/start for no-RAM snapshot). Raises on failure.

    Waits for the guest agent after the restart: whatever runs next dialled the box
    the moment start_vm returned and failed in ~1s against a still-booting Windows
    guest (win02-golden's IIS FTP plant, 2026-10-04 — the rollback+re-plant cycle
    failed the exact step that had succeeded on the first, fully-waited plant)."""

    if vm_status(node, vmid) == "running":
        stop_vm(node, vmid)
    run_vm_task(node, vmid, "POST", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback",
                timeout=timeout)
    if restart:
        start_vm(node, vmid)
        try:
            from infra.guest_exec import wait_for_guest_agent
            wait_for_guest_agent(node, vmid, timeout=600)
        except Exception as e:
            print(f"    WARNING: guest agent wait after rollback of {vmid}: {e}")


def snapshot_support_hint(node, vmid):
    """One-line explanation for why snapshots may be unavailable, for error messages."""
    return (
        f"vmid {vmid} on node {node} has no tezcatlipoca snapshots. Either this range was "
        f"deployed before snapshotting was added, or TEZ_PROXMOX_DATASTORE doesn't support "
        f"snapshots (thick LVM can't; qcow2 on file storage, ZFS, LVM-thin and Ceph can). "
        f"Use --mode reconfigure (re-runs configuration on the live box) or --mode rebuild "
        f"(recreates the VM from its template)."
    )
