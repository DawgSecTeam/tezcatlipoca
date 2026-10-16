"""The per-competition engine template: find / build / destroy (API-driven, mirrors golden_ops)."""

import fcntl
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from common.constants import (
    ENGINE_TEMPLATE_NAME,
    ownership_tags,
)
from infra.client import proxmox_api, wait_for_proxmox_task
from infra.lifecycle import start_vm, stop_vm
from infra.ownership import destroy_vm_if_exists
from engine.template_freeze import engine_template_vmid, write_template_hash
from common.utils import competition_name, record_degradation
from multinode.placement_record import resolved_mgmt_bridge
from estate import config


@contextmanager
def base_clone_lock(node, base_engine_vm_id, wait_s=900):
# Serialise this host's clones of ONE base image.
#
#     PVE holds a datastore lock for the duration of a clone and reports `can't lock file
#     '/var/lock/pve-manager/pve-storage-ssd' - got timeout` to everyone else, so concurrent
#     ranges do not queue — they fight, and the loser burns its retry budget (live 2026-10-16,
#     the second three-range campaign: the loser of three same-image clones needed four of its
#     five attempts and ten minutes while the other two built). Retrying harder is a worse
#     answer than not racing: our own deploys take this lock, so the only contention left is a
#     clone we do not control (another host, or a manual run on older code), which the retry
#     loop below still absorbs.
#
#     Bounded on purpose: a holder that is alive but wedged must not block a deploy forever, so
#     a wait past wait_s proceeds anyway and leaves the PVE lock to the retry.
    endpoint = (config.active().proxmox.endpoint or "unknown")
    host = re.sub(r"^https?://", "", endpoint).split("/")[0].split(":")[0] or "unknown"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", f"{host}-{base_engine_vm_id}")
    path = Path.home() / ".tezcatlipoca" / "locks" / f"base-clone-{slug}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        deadline, announced = time.time() + wait_s, False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    print(f"    base-image clone lock held >{wait_s}s — proceeding anyway; "
                          f"the clone retry absorbs the contention")
                    break
                if not announced:
                    holder = (path.read_text().strip() or "?")
                    print(f"    waiting for the base-image clone lock (held by pid {holder}) — "
                          f"queuing instead of racing for it")
                    announced = True
                time.sleep(5)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def find_engine_template(node, engine_vmid):
# vmid of this competition's engine template, or None. Identified by the reserved
#     vmid; ownership-tagged like everything else we create.
    vmid = engine_template_vmid(engine_vmid)
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        return None
    return vmid if cfg.get("template") else None


def build_engine_template(node, comp_dir, engine_vmid, base_engine_vm_id, ctx,
                          postgres_password, redis_password, quotient_ref,
                          hash_value, inputs, run_id=None, mgmt_bridge=""):
# Build the per-competition engine template (API-driven, mirroring golden_ops —
#     the deploy only consumes the finished template as the phase-4 clone source).
#
#     Template point: after bootstrap (packages, Docker, Quotient images, apt-cacher-ng)
#     and BEFORE any competition state exists. Before conversion: containers stopped and
#     every data volume removed (no scoring DB/teams/injects baked in), /opt/quotient/.env
#     and host keys removed, /etc/machine-id truncated and cloud-init cleaned — each clone
#     gets a fresh identity and fresh host keys. The apt-cacher-ng cache on the template is
#     EMPTY (nothing has used the cacher at build time); the cache that speeds prep_apt is
#     built on the linked-clone engine during a deploy and dies with it.
    from engine.engine_bootstrap_ops import bootstrap_scoring_engine
    from engine.engine_identity_ops import clean_engine_for_template
    from common.ssh_ops import forget_engine_host_key, wait_for_ssh

    vmid = engine_template_vmid(engine_vmid)
    vm_name = ENGINE_TEMPLATE_NAME
    expect_tags = ownership_tags(competition_name(comp_dir), run_id, "engine-template")
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

    # The full clone of the base image is the deploy's first storage-heavy call, and three
    # concurrent ranges issue it within seconds of each other: PVE serializes them on one
    # datastore lock and reports `clone failed: can't lock file
    # '/var/lock/pve-manager/pve-storage-ssd' - got timeout` to the loser (live-found
    # 2026-10-15, all three campaign deploys). The operation is idempotent, so retry it,
    # reclaiming the locked partial clone (whose name proves it ours) in between.
    #
    # The retry must OUTLAST the contention window, not merely repeat: a flat 20 s gap kept
    # landing inside it — the loser of a three-way race failed at +0:03, +0:36 and +1:11 while
    # the two winners' clones committed seconds apart at the start (run-2631ec27, 2026-10-15;
    # the winner's own clone task completes in ~1 s, so what is contended is the serialised
    # allocation queue behind the lock, not one long clone). Backing off 40 s, 80 s, 120 s
    # gives the queue ~6.5 minutes to drain before the last attempt.
    with base_clone_lock(node, base_engine_vm_id):
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
        # cloud-init identity for the build VM's first boot (the base image bakes none;
        # the create path's cloud-init normally supplies it for the deployed engine).
        "ciuser": ctx.get("vm_username", "ubuntu"),
        "sshkeys": ctx.get("ssh_public_key_quoted", ""),
        "cores": 4,
        "memory": 4096,
    }
    if mgmt_ip:
        # Portable-node mode (realm): boot the build VM on the PLANNED engine mgmt IP —
        # no leftover engine holds it (a fresh deploy refuses one), so it is free,
        # and the converted template's config then matches what apply #1 sets on clones.
        gw = config.active().engine.mgmt_gw
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
        # Primary-mode: the base image networks itself (as the deployed engine does
        # today); discover the address via the guest agent once the agent is up.
        from infra.guest_exec import wait_for_guest_agent
        wait_for_guest_agent(node, vmid, timeout=300)
        build_ip = _discover_vm_ipv4(node, vmid)
        if not build_ip:
            raise RuntimeError("could not discover the engine-template build VM's IP — "
                               "set TEZ_ENGINE_MGMT_IP (portable-node mode) or fix "
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
    # real settle condition first — which now includes cloud-init's own boot-time
    # package_upgrade being finished: the apt-holder check alone slips through the
    # gaps between cloud-init's apt invocations and the next one bounces sshd
    # mid-build (2026-10 kernel+firmware batch). On realm the guest agent returns
    # NULL data, so this exercises the SSH settle fallback.
    from guests.box_settle_ops import wait_boxes_settled, _box_settled_via_ssh
    # Stamp the build VM before anything else touches it: every later SSH goes to a
    # management IP that another competition's engine can share, and the template clean
    # is the most destructive command in the pipeline (known-issues: the wrong-engine
    # wipe). A wrong machine now fails the identity check instead of losing its state.
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
# First non-loopback IPv4 the guest agent reports (primary-mode build VMs DHCP on
#     vmbr0, exactly as the deployed engine does). Never raises.
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
# Remove this competition's engine template. Callers destroy its clones first.
#     A FOREIGN VM in the slot is skipped with a loud warning instead of aborting the
#     teardown — same policy as destroy_golden_set.
    try:
        destroy_vm_if_exists(node, engine_template_vmid(engine_vmid), expect_tags=expect_tags)
    except RuntimeError as e:
        print(f"    WARNING: engine template slot is FOREIGN — skipping it and "
              f"continuing: {e}")
