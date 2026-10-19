"""The per-competition engine template: find / build / destroy (API-driven, mirrors golden_ops)."""

import time
from pathlib import Path

import requests

from common.constants import (
    ENGINE_TEMPLATE_NAME,
    ownership_tags,
)
from infra.client import proxmox_api, storage_queue_lock, wait_for_proxmox_task
from infra.lifecycle import start_vm, stop_vm
from infra.ownership import (MARKER, MISSING, OWNED, comp_name_from_tags,
                             destroy_vm_if_exists, vm_occupancy)
from engine.template_freeze import engine_template_vmid, write_template_hash
from common.utils import competition_name, record_degradation
from multinode.placement_record import resolved_mgmt_bridge
from estate import config



def find_engine_template(node, engine_vmid):
    vmid = engine_template_vmid(engine_vmid)
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        return None
    return vmid if cfg.get("template") else None


def build_engine_template(node, comp_dir, engine_vmid, base_engine_vm_id, ctx,
                          postgres_password, redis_password, quotient_ref,
                          hash_value, inputs, run_id=None, mgmt_bridge=""):
# The apt-cacher-ng cache is empty here; it builds on the linked-clone engine at deploy time.
    from engine.engine_bootstrap_ops import bootstrap_scoring_engine
    from engine.engine_identity_ops import clean_engine_for_template
    from common.ssh_ops import forget_engine_host_key, wait_for_ssh

    vmid = engine_template_vmid(engine_vmid)
    vm_name = ENGINE_TEMPLATE_NAME
    # Kept engine templates carry an earlier run's id; the hash-drift rebuild reclaims
    # this competition's template, not another competition's.
    expect_tags = ownership_tags(competition_name(comp_dir), None, "engine-template")
    tags = ",".join(sorted(expect_tags))

    # A dead build leaves an unconverted VM on the reserved slot wearing the base image's
    # inherited tags; the reserved NAME proves it is our attempt, not a foreign VM.
    from golden.golden_target_ops import _is_template  # local: golden_ops imports this module back

    if vmid in {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]} \
            and not _is_template(node, vmid):
        vm = next(v for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                  if v["vmid"] == vmid)
        if (vm.get("name") or "") == ENGINE_TEMPLATE_NAME:
            print(f"    vmid {vmid}: unconverted '{ENGINE_TEMPLATE_NAME}' leftover from a "
                  f"dead template build — adopting and rebuilding")
            destroy_vm_if_exists(node, vmid, expect_tags=None)
    destroy_vm_if_exists(node, vmid, expect_tags=expect_tags)

    # PVE reports `can't lock file ... got timeout` to losers of concurrent full clones;
    # the backoff must outlast the contention window, hence growing 40/80/120s waits.
    with storage_queue_lock(node):
        for attempt in range(5):
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{base_engine_vm_id}/clone", data={
                "newid": vmid,
                "name": vm_name,
                "full": 1,
            })["data"]
            try:
                wait_for_proxmox_task(node, upid)
                break
            except RuntimeError as e:
                if "lock file" not in str(e) or attempt == 4:
                    raise
                print(f"    clone of base image {base_engine_vm_id} hit datastore lock "
                      f"contention — reclaiming the partial clone and retrying "
                      f"(attempt {attempt + 2}/5)")
                leftover = next((v for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                                 if v["vmid"] == vmid and (v.get("name") or "") == vm_name), None)
                if leftover:
                    destroy_vm_if_exists(node, vmid, expect_tags=None)
                time.sleep(40 * (attempt + 1))

    mgmt_ip = config.active().engine.mgmt_ip
    cfg = {
        "net0": f"virtio,bridge={mgmt_bridge or resolved_mgmt_bridge()}",
        "tags": tags,
        "ciuser": ctx.get("vm_username", "ubuntu"),
        "sshkeys": ctx.get("ssh_public_key_quoted", ""),
        "cores": 4,
        "memory": 4096,
    }
    if mgmt_ip:
        # Boot on the planned mgmt IP so the template's config matches what clones get; a
        # fresh deploy refuses a leftover engine holding it.
        gw = config.active().engine.mgmt_gw
        cfg["ipconfig0"] = f"ip={mgmt_ip}/24" + (f",gw={gw}" if gw else "")
    # A PUT landing right after the clone commits can 400 "Parameter verification failed"
    # while pvefs publishes the new VM's config; the identical payload succeeds seconds later.
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

    # The base image's root is ~10 GB; the compose build fills it and the build dies
    # (live: run-b789543d phase 2). Size the disk before the bootstrap grows the fs.
    from estate.apply import ENGINE_DISK_GB, ensure_disk_size
    ensure_disk_size(node, vmid, ENGINE_DISK_GB)

    start_vm(node, vmid)
    build_ip = mgmt_ip
    if not build_ip:
        from infra.guest_exec import wait_for_guest_agent
        wait_for_guest_agent(node, vmid, timeout=300)
        build_ip = _discover_vm_ipv4(node, vmid)
        if not build_ip:
            raise RuntimeError("could not discover the engine-template build VM's IP — "
                               "set TEZ_ENGINE_MGMT_IP (portable-node mode) or fix "
                               "the agent channel")
    forget_engine_host_key(build_ip)
    # 900s: parallel deploys saturating the node pushed first-boot past 300s, twice.
    if not wait_for_ssh(ctx["ssh_key_path"], ctx.get("vm_username", "ubuntu"), build_ip,
                        timeout=900):
        raise RuntimeError(f"engine-template build VM {build_ip} never accepted SSH")
    build_ctx = {**ctx, "scoring_engine_ip": build_ip,
                 "box_username": ctx.get("vm_username", "ubuntu")}

    # The apt-holder check alone slips through the gaps between cloud-init's own
    # boot-time package_upgrade apt invocations, and the next one bounces sshd mid-build.
    from guests.box_settle_ops import wait_boxes_settled, _box_settled_via_ssh
    # Every later SSH goes to a possibly-shared mgmt IP and the template clean is the most
    # destructive command in the pipeline; a wrong machine must fail the identity check.
    from engine.engine_identity_ops import stamp_engine_build
    stamp_engine_build(build_ctx, vmid)

    print("    Waiting for the build VM to settle (cloud-init + unattended-upgrades done, dpkg lock free)...")
    unsettled = wait_boxes_settled([{"vmid": vmid, "ip": build_ip}], node, timeout=600,
                                   ssh_fallback=lambda t: _box_settled_via_ssh(build_ctx, t),
                                   cloud_init_ctx=build_ctx)
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


OTHER_RUN = "another run of this competition"


def destroy_engine_template(node, engine_vmid, expect_tags=None):
    """Destroy this run's engine template; return skip messages for anything else."""
    vmid = engine_template_vmid(engine_vmid)
    verdict, tags, vm = vm_occupancy(node, vmid, expect_tags)
    if verdict == "absent":
        return []
    if verdict in (OWNED, MARKER):
        destroy_vm_if_exists(node, vmid, expect_tags=expect_tags)
        return []
    if verdict == "unknown":
        return [f"vmid {vmid} could not be classified (unreadable) — check by hand"]
    comp = comp_name_from_tags(expect_tags or ())
    if verdict == MISSING and comp and f"comp-{comp}" in tags:
        message = (f"{OTHER_RUN} holds vmid {vmid} "
                   f"({vm.get('name') or '?'}, tags: {';'.join(sorted(tags))}) — not touched")
    else:
        message = (f"vmid {vmid} is occupied by a foreign VM ({vm.get('name') or '?'}, "
                   f"tags: {';'.join(sorted(tags)) or 'none'}) — skipped")
    print(f"    WARNING: engine template slot: {message}")
    return [message]
