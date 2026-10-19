"""VM power state and snapshot helpers (no ownership decisions in here)."""

from infra.client import proxmox_api, run_vm_task
from common.utils import record_degradation

# PVE's own ACPI-shutdown wait (POST /status/shutdown `timeout`); it must expire inside OUR client
# wait — abandoning a still-running qmshutdown and force-stopping hits the lock that task holds.
SHUTDOWN_GRACE_S = 120

# PVE's own wait for a start; same abandon-the-task hazard as SHUTDOWN_GRACE_S.
START_GRACE_S = 900

def vm_status(node, vmid):
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


def start_vm(node, vmid, timeout=START_GRACE_S):
    status = vm_status(node, vmid)
    if status is None or status == "running":
        return
    try:
        run_vm_task(node, vmid, "POST", f"/nodes/{node}/qemu/{vmid}/status/start",
                    data={"timeout": timeout}, timeout=timeout + 120)
    except RuntimeError:
        # a task that outlived the wait may still have started the VM; recheck before failing
        if vm_status(node, vmid) != "running":
            raise


def list_snapshots(node, vmid):
    """Proxmox lists a synthetic `current` snapshot; it is excluded."""
    try:
        data = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/snapshot")["data"]
    except Exception:
        return set()
    return {s["name"] for s in data if s.get("name") != "current"}


def delete_snapshot(node, vmid, name, timeout=600):
    run_vm_task(node, vmid, "DELETE", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}",
                timeout=timeout)


def take_snapshot(node, vmid, name, description="", timeout=900):
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
        # tz-base/tz-ready are the rollback points: a failed snapshot means no rollback,
        # and a failed golden one drops the pre-plant guard.
        record_degradation(f"snapshot '{name}' failed",
                           f"vmid {vmid}: {str(e)[:200]}")
        return False


def rollback_snapshot(node, vmid, name, timeout=900, restart=True):
    if vm_status(node, vmid) == "running":
        stop_vm(node, vmid)
    run_vm_task(node, vmid, "POST", f"/nodes/{node}/qemu/{vmid}/snapshot/{name}/rollback",
                timeout=timeout)
    if restart:
        start_vm(node, vmid)
        try:
            # callers dial the moment start_vm returns; a Windows guest is still booting
            from infra.guest_exec import wait_for_guest_agent
            wait_for_guest_agent(node, vmid, timeout=600)
        except Exception as e:
            print(f"    WARNING: guest agent wait after rollback of {vmid}: {e}")


def snapshot_support_hint(node, vmid):
    return (
        f"vmid {vmid} on node {node} has no tezcatlipoca snapshots. Either this range was "
        f"deployed before snapshotting was added, or TEZ_PROXMOX_DATASTORE doesn't support "
        f"snapshots (thick LVM can't; qcow2 on file storage, ZFS, LVM-thin and Ceph can). "
        f"Use --mode reconfigure (re-runs configuration on the live box) or --mode rebuild "
        f"(recreates the VM from its template)."
    )
