"""Golden boot smoke gate: throwaway full clone must boot before a golden is converted."""

import subprocess
import time

from constants import FINAL_STAGE_CONFIGS, GOLDEN_CLONE_TIMEOUT, ownership_tags
from infra.client import cluster_vms_for, proxmox_api, wait_for_proxmox_task
from infra.guest_exec import wait_for_guest_agent
from infra.lifecycle import start_vm
from infra.ownership import destroy_vm_if_exists, gc_orphan_volumes
from ssh_ops import ssh_via_gateway
from infra.ownership import full_clone_data
from windows_ops import is_windows_template


BOOT_SMOKE_PASS = "verified-bootable"
BOOT_SMOKE_UNBOOTABLE = "verified-unbootable"
BOOT_SMOKE_UNVERIFIED = "could-not-verify"
GOLDEN_BOOT_SMOKE_TAG = "tezcatlipoca-golden-bootsmoke"
# Deliberately generous bounds: they only cost wall clock on a golden that is genuinely
# failing (a healthy clone returns as soon as it answers). A first boot of a fresh clone
# can legitimately crawl on the overprovisioned thin pool, and bootstrap_windows_box
# already budgets 1800s for a Windows first boot.
GOLDEN_BOOT_SMOKE_TIMEOUT = 1200
GOLDEN_BOOT_SMOKE_WIN_TIMEOUT = 1800
GOLDEN_BOOT_SMOKE_POLL = 15


# PVE's own allocator (/cluster/nextid, as template_sync_ops also uses) plus a
# node-local existence check: with a multi-node placement the allocator may be
# answered by the primary endpoint while this golden lives on a satellite host,
# where that id can already be taken.
def _smoke_vmid(node):
    existing = {int(v["vmid"]) for v in cluster_vms_for(node)}
    try:
        vmid = int(proxmox_api("GET", "/cluster/nextid")["data"])
    except Exception:
        vmid = max(existing) + 1 if existing else 900
    while vmid in existing:
        vmid += 1
    return vmid


# Identity is the clone NAME (unique to this build) plus its full ownership tag set
# (comp tag + this run's run tag): never bare vmid math. A vmid we created but that
# now carries another name is left alone, loudly.
def _destroy_smoke_clone(node, vmid, name, comp_dir, run_id=None):
    try:
        vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    except Exception as e:
        print(f"    WARNING: boot-smoke clone vmid {vmid} state unreadable ({str(e)[:120]}) "
              f"— destroy it manually before the next deploy")
        return
    vm = next((v for v in vms if int(v["vmid"]) == vmid), None)
    if vm is None:
        gc_orphan_volumes(node, vmid)  # an interrupted clone can strand its volumes
        return
    if vm.get("name") != name:
        print(f"    WARNING: vmid {vmid} is '{vm.get('name')}', not our boot-smoke clone "
              f"'{name}' — leaving it alone; destroy it manually if it is a stranded clone")
        return
    destroy_vm_if_exists(node, vmid, expect_tags=ownership_tags(comp_dir.name, run_id))


# The explicit multi-user check makes the 2026-09-24 signature (systemd up, no
# multi-user.target because it is masked) legible in the failure detail instead of a
# bare timeout; the Windows guest-agent answer is the same readiness signal
# wait_for_boxes_ssh uses. An exception that waiting cannot fix (bad ctx/key, no ssh
# binary) is UNVERIFIED — immediate, never a silent pass and never a full-budget spin.
def _probe_boot(node, vmid, target, ctx, timeout, poll):
    if is_windows_template(target["box"]["template"]):
        if wait_for_guest_agent(node, vmid, timeout=timeout):
            return BOOT_SMOKE_PASS, "guest agent answered"
        return BOOT_SMOKE_UNBOOTABLE, f"guest agent never answered within {timeout}s"
    deadline = time.time() + timeout
    detail = "no probe attempt completed within the budget"
    while time.time() < deadline:
        try:
            r = ssh_via_gateway(ctx, target["ip"], "systemctl is-active multi-user.target",
                                timeout=20, user=ctx.get("box_username", "ubuntu"))
        except subprocess.TimeoutExpired:
            detail = "ssh probe timed out (box not answering yet)"
        except Exception as e:
            return BOOT_SMOKE_UNVERIFIED, (f"probe could not run ({type(e).__name__}: "
                                           f"{str(e)[:120]})")
        else:
            state = (r.stdout or "").strip().splitlines()
            state = state[-1] if state else ""
            if r.returncode == 0 and state == "active":
                return BOOT_SMOKE_PASS, "multi-user.target active over SSH"
            detail = (f"ssh rc={r.returncode} multi-user.target={state or '?'} "
                      f"{(r.stderr or '').strip()[:100]}")
        time.sleep(poll)
    return BOOT_SMOKE_UNBOOTABLE, f"{detail} (gave up after {timeout}s)"


def _boot_smoke_error(box, target, status, detail, planted_configs):
    hostile = sorted(set(planted_configs) & FINAL_STAGE_CONFIGS)
    lines = [
        f"golden boot smoke FAILED ({status}) for box type '{box}': the throwaway clone of "
        f"golden vmid {target['vmid']} did not reach multi-user ({detail}).",
        "Refusing to convert this golden into a template: a golden disk that cannot boot "
        "leaves EVERY linked team clone unbootable — one phase later, after every box type "
        "has been built.",
        "Likely cause: a boot-hostile config rode the golden stage. constants."
        "FINAL_STAGE_CONFIGS (a subset of POST_CLONE_CONFIGS) lists the configs that must "
        "plant after cloning; the 2026-09-24 incident was 'systemd-system-masked', which "
        "returns rc=0 while masking multi-user/graphical/default targets — systemd then "
        "boots with no multi-user.target, so no cloud-init and no network "
        "(docs/benchmark-m02.md). strict=True cannot see "
        "it because the plant step exits 0.",
    ]
    if hostile:
        lines.append("This golden-stage plan explicitly carries boot-hostile config(s): "
                     + ", ".join(hostile) + " — move them to the repair/final stage.")
    return " ".join(lines)


# Why a throwaway FULL clone, not the golden itself: rebooting the golden would mutate
# the very disk about to be templated (cloud-init identity/hostname/IP), and PVE only
# creates linked clones from templates — the point is to verify BEFORE the golden
# becomes one. One clone + boot per box type, run sequentially with the golden stopped.
# Tri-state, fail-closed: PASS returns True; UNBOOTABLE and UNVERIFIED both raise. A
# clone that cannot be created or a probe that cannot run is a FAILURE, never a pass —
# this project's gates have failed open before, and that is exactly how the 2026-09-24
# incident reached the clone phase.
def golden_boot_smoke(node, target, ctx, comp_dir, timeout=None,
                      poll=GOLDEN_BOOT_SMOKE_POLL, planted_configs=(), run_id=None):
    box = target["box"]["name"]
    windows = is_windows_template(target["box"]["template"])
    if timeout is None:
        timeout = GOLDEN_BOOT_SMOKE_WIN_TIMEOUT if windows else GOLDEN_BOOT_SMOKE_TIMEOUT
    try:
        vmid = _smoke_vmid(node)
    except Exception as e:
        raise RuntimeError(
            f"golden boot smoke COULD NOT VERIFY box '{box}': no vmid could be allocated "
            f"for the throwaway clone ({type(e).__name__}: {str(e)[:160]}) — refusing to "
            f"convert an unverified golden to a template") from e
    name = f"{target['vm_name']}-bootsmoke"
    print(f"  Boot smoke: booting a throwaway clone of {target['vm_name']} "
          f"(vmid {target['vmid']}) as vmid {vmid} to prove the golden disk reaches "
          f"multi-user (timeout {timeout}s)...")
    try:
        try:
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{target['vmid']}/clone",
                               data=full_clone_data(vmid, name, comp_dir.name))["data"]
            wait_for_proxmox_task(node, upid, timeout=GOLDEN_CLONE_TIMEOUT)
        except Exception as e:
            raise RuntimeError(
                f"golden boot smoke COULD NOT VERIFY box '{box}': the throwaway clone of "
                f"golden vmid {target['vmid']} could not be created "
                f"({type(e).__name__}: {str(e)[:160]}) — refusing to convert an unverified "
                f"golden to a template, because every linked clone of it might be "
                f"unbootable") from e
        # The clone inherits net0/ipconfig0/ciuser/cipassword/sshkeys from the golden, so
        # it comes up on the golden's address (free: the golden is stopped here) exactly
        # as a team clone comes up on its own after the golden's cloud-init clean.
        try:
            proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data={
                "tags": ",".join(sorted(ownership_tags(
                    comp_dir.name, run_id, GOLDEN_BOOT_SMOKE_TAG)))})
        except Exception as e:
            # Cosmetic only; teardown identifies the clone by name.
            print(f"    WARNING: could not tag boot-smoke clone vmid {vmid}: {str(e)[:120]}")
        try:
            start_vm(node, vmid)
        except Exception as e:
            raise RuntimeError(
                f"golden boot smoke COULD NOT VERIFY box '{box}': throwaway clone vmid "
                f"{vmid} would not start ({type(e).__name__}: {str(e)[:160]}) — refusing to "
                f"convert an unverified golden to a template") from e
        status, detail = _probe_boot(node, vmid, target, ctx, timeout, poll)
        if status != BOOT_SMOKE_PASS:
            raise RuntimeError(_boot_smoke_error(box, target, status, detail, planted_configs))
        print(f"    {box}: throwaway clone (vmid {vmid}) reached multi-user — golden disk boots")
        return True
    finally:
        try:
            _destroy_smoke_clone(node, vmid, name, comp_dir, run_id=run_id)
        except Exception as e:
            # Never mask the smoke verdict with a teardown error — but leaking a VM is
            # loud, not silent.
            print(f"    WARNING: failed to destroy boot-smoke clone vmid {vmid} "
                  f"({str(e)[:120]}) — destroy it manually before the next deploy")
