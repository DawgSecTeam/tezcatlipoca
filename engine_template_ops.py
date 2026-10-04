"""The per-competition engine template: find / build / destroy (API-driven, mirrors golden_ops)."""

import os
import time
from pathlib import Path

import requests

from constants import (
    ENGINE_TEMPLATE_NAME,
    ownership_tags,
)
from pve_api import proxmox_api, wait_for_proxmox_task
from vm_lifecycle import start_vm, stop_vm
from vm_ownership import destroy_vm_if_exists
from template_freeze import engine_template_vmid, write_template_hash
from utils import record_degradation


def find_engine_template(node, engine_vmid):
    """vmid of this competition's engine template, or None. Identified by the reserved
    vmid; ownership-tagged like everything else we create."""
    vmid = engine_template_vmid(engine_vmid)
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        return None
    return vmid if cfg.get("template") else None


def build_engine_template(node, comp_dir, engine_vmid, base_engine_vm_id, ctx,
                          postgres_password, redis_password, quotient_ref,
                          hash_value, inputs, run_id=None):
    """Build the per-competition engine template (API-driven, mirroring golden_ops —
    terraform only consumes the finished template as apply #1's clone source).

    Template point: after bootstrap (packages, Docker, Quotient images, apt-cacher-ng)
    and BEFORE any competition state exists. Before conversion: containers stopped and
    every data volume removed (no scoring DB/teams/injects baked in), /opt/quotient/.env
    and host keys removed, /etc/machine-id truncated and cloud-init cleaned — each clone
    gets a fresh identity and fresh host keys. The apt-cacher-ng cache on the template is
    EMPTY (nothing has used the cacher at build time); the cache that speeds prep_apt is
    built on the linked-clone engine during a deploy and dies with it."""
    from engine_ops import bootstrap_scoring_engine, clean_engine_for_template
    from ssh_ops import forget_engine_host_key, wait_for_ssh

    vmid = engine_template_vmid(engine_vmid)
    vm_name = ENGINE_TEMPLATE_NAME
    expect_tags = ownership_tags(Path(comp_dir).name, run_id, "engine-template")
    tags = ",".join(sorted(expect_tags))

    # A previous build's leftover (plain VM, never converted) is a dead attempt:
    # rebuild it from scratch rather than resuming a half-bootstrapped disk. An
    # untagged leftover passes with the ownership warning; anything tagged without
    # ours is refused — the reserved slot is not a license to destroy foreign VMs.
    # Exception (live-found 2026-09-29): a full clone inherits the BASE image's tags,
    # so a config-PUT failure between the clone and the tag PUT leaves a leftover
    # wearing 'cloud-init;template' — the strict guard would refuse it forever. A
    # NOT-yet-converted VM on the reserved slot carrying the reserved NAME is our
    # dead attempt; adopt it loudly. Converted templates keep the strict check.
    from golden_ops import _is_template  # local: golden_ops imports this module back

    if vmid in {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]} \
            and not _is_template(node, vmid):
        vm = next(v for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                  if v["vmid"] == vmid)
        if (vm.get("name") or "") == ENGINE_TEMPLATE_NAME:
            print(f"    vmid {vmid}: unconverted '{ENGINE_TEMPLATE_NAME}' leftover from a "
                  f"dead template build — adopting and rebuilding")
            destroy_vm_if_exists(node, vmid, expect_tags=None)
    destroy_vm_if_exists(node, vmid, expect_tags=expect_tags)

    live_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
    if vmid in live_vmids:
        raise RuntimeError(f"engine template vmid {vmid} is occupied and not ours to remove")
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{base_engine_vm_id}/clone", data={
        "newid": vmid,
        "name": vm_name,
        "full": 1,
    })["data"]
    wait_for_proxmox_task(node, upid)

    mgmt_ip = os.environ.get("TF_VAR_engine_mgmt_ip", "")
    cfg = {
        "net0": "virtio,bridge=vmbr0",
        "tags": tags,
        # cloud-init identity for the build VM's first boot (the base image bakes none;
        # terraform's user_account normally supplies it for the deployed engine).
        "ciuser": ctx.get("vm_username", "ubuntu"),
        "sshkeys": ctx.get("ssh_public_key_quoted", ""),
        "cores": 4,
        "memory": 4096,
    }
    if mgmt_ip:
        # Portable-node mode (realm): boot the build VM on the PLANNED engine mgmt IP —
        # the deployed engine is destroyed by phase 1 by now, so the address is free,
        # and the converted template's config then matches what apply #1 sets on clones.
        gw = os.environ.get("TF_VAR_engine_mgmt_gw", "")
        cfg["ipconfig0"] = f"ip={mgmt_ip}/24" + (f",gw={gw}" if gw else "")
    # A PUT landing immediately after the clone task commits can 400 with a bare
    # "Parameter verification failed" while pvefs finishes publishing the new VM's
    # config (amongus-cde-2026 2026-09-30: three deploys in a row died here; the
    # identical payload succeeded against the same vmid seconds later). Retry the
    # transient window, and surface the response body if it's a real rejection.
    for attempt in range(4):
        try:
            proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config", data=cfg)
            break
        except requests.HTTPError as e:
            body = ""
            if e.response is not None:
                body = e.response.text[:300]
                if e.response.status_code != 400:
                    raise
            if attempt == 3:
                raise RuntimeError(
                    f"engine-template config PUT kept failing on vmid {vmid}: {body}") from e
            time.sleep(2 * (attempt + 1))
    print(f"    {vm_name} cloned from base image (vmid {vmid})")

    start_vm(node, vmid)
    build_ip = mgmt_ip
    if not build_ip:
        # Primary-mode: the base image networks itself (as the terraform engine does
        # today); discover the address via the guest agent once the agent is up.
        from range_ops import wait_for_guest_agent
        wait_for_guest_agent(node, vmid, timeout=300)
        build_ip = _discover_vm_ipv4(node, vmid)
        if not build_ip:
            raise RuntimeError("could not discover the engine-template build VM's IP — "
                               "set TF_VAR_engine_mgmt_ip (portable-node mode) or fix "
                               "the agent channel")
    forget_engine_host_key(build_ip)
    # 300s lost twice to first-boot-under-load (parallel deploy saturating the node
    # while this clone regenerates host keys + applies cloud-init; amongus-cde-2026
    # 2026-09-30 — manual SSH succeeded minutes after each timed-out wait).
    if not wait_for_ssh(ctx["ssh_key_path"], ctx.get("vm_username", "ubuntu"), build_ip,
                        timeout=900):
        raise RuntimeError(f"engine-template build VM {build_ip} never accepted SSH")
    build_ctx = {**ctx, "scoring_engine_ip": build_ip,
                 # the settle probe and every bootstrap SSH authenticate as the
                 # engine's own user, not the default box user
                 "box_username": ctx.get("vm_username", "ubuntu")}

    # A fresh base-image clone runs unattended-upgrades on first boot; the old
    # engine bootstrap's killall+sleep-2 preamble raced it and lost (live-found
    # 2026-09-25: apt rc=100, dpkg lock held by the image's own apt). Wait for the
    # real settle condition first. On realm the guest agent returns NULL data, so
    # this exercises the SSH settle fallback.
    from hardening_ops import wait_boxes_settled, _box_settled_via_ssh
    # Stamp the build VM before anything else touches it: every later SSH goes to a
    # management IP that another competition's engine can share, and the template clean
    # is the most destructive command in the pipeline (known-issues: the wrong-engine
    # wipe). A wrong machine now fails the identity check instead of losing its state.
    from engine_ops import stamp_engine_build
    stamp_engine_build(build_ctx, vmid)

    print("    Waiting for the build VM to settle (unattended-upgrades done, dpkg lock free)...")
    unsettled = wait_boxes_settled([{"vmid": vmid, "ip": build_ip}], node, timeout=600,
                                   ssh_fallback=lambda t: _box_settled_via_ssh(build_ctx, t))
    if unsettled:
        print("    WARNING: build VM never fully settled — proceeding (the bootstrap's "
              "Lock::Timeout covers a straggler)")
        record_degradation("engine build VM never fully settled",
                           f"vmid {vmid}")

    print("    Bootstrapping engine template (packages, Docker, Quotient, apt-cacher-ng)...")
    build_info = bootstrap_scoring_engine(build_ctx, postgres_password, redis_password,
                                          quotient_ref=quotient_ref)

    print("    Cleaning engine template for conversion (fresh state per clone)...")
    clean_engine_for_template(build_ctx, vmid)
    stop_vm(node, vmid)
    proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/template")["data"]
    write_template_hash(node, vmid, hash_value,
                        extra=f"quotient_ref={quotient_ref or 'default'}")
    print(f"    {vm_name} (vmid {vmid}) is now a template")
    return vmid, build_info


def _discover_vm_ipv4(node, vmid):
    """First non-loopback IPv4 the guest agent reports (primary-mode build VMs DHCP on
    vmbr0, exactly as the terraform engine does). Never raises."""
    try:
        ifaces = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )["data"]["result"]
        for iface in ifaces:
            for a in iface.get("ip-addresses", []):
                if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127."):
                    return a["ip-address"]
    except Exception:
        pass
    return None


def destroy_engine_template(node, engine_vmid, expect_tags=None):
    """Remove this competition's engine template. Callers destroy its clones first.
    A FOREIGN VM in the slot is skipped with a loud warning instead of aborting the
    teardown — same policy as destroy_golden_set."""
    try:
        destroy_vm_if_exists(node, engine_template_vmid(engine_vmid), expect_tags=expect_tags)
    except RuntimeError as e:
        print(f"    WARNING: engine template slot is FOREIGN — skipping it and "
              f"continuing: {e}")
