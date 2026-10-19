"""Golden-set build and template conversion: plant once per box type, convert to a template, link-clone teams from it."""

import json
import time
from pathlib import Path

from common.constants import GOLDEN_CLONE_TIMEOUT, GOLDEN_TAG, SNAP_BASE, ownership_tags
from guests.alpine_ops import ensure_alpine_services
from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
from guests.ubuntu_auth_ops import setup_ubuntu_auth
from nakonops.nakon_bundle_ops import build_nakon_bundle
from nakonops.nakon_run_ops import run_nakon
from infra.ownership import full_clone_data
from infra.client import proxmox_api, storage_queue_lock, wait_for_proxmox_task
from infra.lifecycle import (delete_snapshot, list_snapshots, rollback_snapshot, start_vm, stop_vm, take_snapshot,)
from infra.ownership import (MARKER, MISSING, OWNED, clone_marker, comp_name_from_tags,
                             destroy_vm_if_exists, gc_orphan_volumes, retag_ownership,
                             vm_occupancy,)
from multinode.placement_record import golden_vmid_for_slot
from common.ssh_ops import ssh_via_gateway, wait_for_boxes_ssh, wait_for_cloud_init
from engine.template_freeze import (
    golden_plant_checkpoints, save_template_hashes, stored_template_hash, write_template_hash,
)
from common.utils import PRINT_LOCK, compfile_flag, competition_name, run_concurrent
from common.windows_ops import bootstrap_windows_box, is_windows_template
from golden.golden_target_ops import (
    _quote_sshkeys,
    _is_template,
    _vm_exists,
    _template_vmid_map,
    golden_targets,
)
from golden.golden_disk_ops import ensure_golden_disk_size, expand_guest_root_disks
from estate import config


def _await_base_volumes(node, vmid, box_name):
# Only base-<vmid>-disk-N volumes can back a linked clone and the rename is asynchronous: a
# template=1 VM still on a plain vm- volume fails at the team clone with PVE 500.
    for _ in range(10):
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        volids = [str(v) for k, v in cfg.items()
                  if k.startswith(("scsi", "sata", "virtio")) and isinstance(v, str) and ":" in v]
        missing = [v for v in volids
                   if not v.split(":", 1)[1].split(",")[0].startswith("base-")]
        if not missing:
            return
        time.sleep(2)
    raise RuntimeError(
        f"golden '{box_name}' (vmid {vmid}) converted but its disk volume(s) {missing} "
        f"never became base- volumes — linked clones would fail with 'Linked clone "
        f"feature is not supported'. Destroy this golden and re-run phase 4 so it is "
        f"rebuilt from scratch.")


def _build_unbooted_goldens(node, cold, templates, comp_dir, own_set, ownership_tag_str,
                           golden_hashes):
    for t in cold:
        # A clone task that outlived its task-wait leaves only the BASE image's inherited tags, which
        # the strict guard refuses forever — adopt one on our slot with our clone marker.
        try:
            cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{t['vmid']}/config")["data"]
            desc = str(cfg.get("description") or "")
            raw_tags = str(cfg.get("tags") or "")
            tags = {x.strip() for x in raw_tags.replace(";", ",").split(",") if x.strip()}
            if (cfg.get("name") == t["vm_name"] and clone_marker(competition_name(comp_dir)) in desc
                    and "tezcatlipoca" not in tags):
                print(f"    vmid {t['vmid']}: untagged dead-attempt clone of "
                      f"'{t['vm_name']}' (our clone marker in description) — adopting")
                destroy_vm_if_exists(node, t["vmid"], expect_tags=None)
        except Exception:
            pass  # slot empty, or unreadable — the strict destroy below decides
        destroy_vm_if_exists(node, t["vmid"], expect_tags=own_set)
        src = templates.get(t["box"]["template"])
        if src is None:
            raise RuntimeError(f"no stopped template named '{t['box']['template']}' on the node")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone",
                           data=full_clone_data(t["vmid"], t["vm_name"], competition_name(comp_dir)))["data"]
        wait_for_proxmox_task(node, upid, timeout=GOLDEN_CLONE_TIMEOUT)
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={
            "net0": f"virtio,bridge={t['bridge']}", "tags": ownership_tag_str})
        proxmox_api("POST", f"/nodes/{node}/qemu/{t['vmid']}/template")
        _await_base_volumes(node, t["vmid"], t["box"]["name"])
        if golden_hashes:
            write_template_hash(node, t["vmid"], golden_hashes[t["box"]["name"]],
                                extra=f"box={t['box']['name']} unbooted")
        print(f"    {t['vm_name']} (vmid {t['vmid']}) is now an UNBOOTED template "
              f"(generalized — each team's clone specializes its own SID)")


def _clone_missing_goldens(node, missing, templates, comp_dir, ctx, box_password,
                          ownership_tag_str):
    print(f"  Cloning {len(missing)} golden box(es) from base templates...")
    # The clone takes PVE's per-datastore lock; without the queue concurrent ranges fought and the
    # loser died with `can't lock file ... got timeout`. Retry covers a foreign holder.
    for t in missing:
        skipped = False
        src = templates.get(t["box"]["template"])
        if src is None:
            raise RuntimeError(f"no stopped template named '{t['box']['template']}' on the node")
        with storage_queue_lock(node):
            for attempt in range(3):
                existing_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
                if t["vmid"] in existing_vmids:
                    locked_cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{t['vmid']}/config")["data"]
                    if not locked_cfg.get("lock"):
                        skipped = True
                        break
                    # An interrupted clone (half-copied disk) is never usable — destroy it, but only if the
                    # description proves it ours.
                    if clone_marker(competition_name(comp_dir)) not in str(locked_cfg.get("description") or ""):
                        raise RuntimeError(
                            f"refusing to destroy locked vmid {t['vmid']} on reserved golden "
                            f"slot '{t['vm_name']}' — no clone marker in its description, "
                            f"not provably ours")
                    destroy_vm_if_exists(node, t["vmid"], expect_tags=None)
                else:
                    gc_orphan_volumes(node, t["vmid"])
                upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone",
                                   data=full_clone_data(t["vmid"], t["vm_name"], competition_name(comp_dir)))["data"]
                try:
                    wait_for_proxmox_task(node, upid, timeout=GOLDEN_CLONE_TIMEOUT)
                    break
                except RuntimeError as e:
                    # A loser may leave a half-clone with `lock: clone`, reaped by the interrupted-clone branch above.
                    if "lock file" not in str(e) or attempt == 2:
                        raise
                    print(f"    {t['vm_name']}: golden clone hit datastore lock contention — "
                          f"retrying (attempt {attempt + 2}/3)")
                    time.sleep(40 * (attempt + 1))
        if skipped:
            continue
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={
            "ipconfig0": f"ip={t['ip']}/24,gw={t['gateway']}",
            "net0": f"virtio,bridge={t['bridge']}",
            "tags": ownership_tag_str,
            "ciuser": ctx.get("box_username", "ubuntu"),
            "cipassword": box_password,
            "sshkeys": _quote_sshkeys(config.active().ssh.public_key),
        })
        ensure_golden_disk_size(node, t["vmid"], t["box"].get("disk_gb"))
        print(f"    {t['box']['template']} -> {t['vm_name']} (vmid {t['vmid']})")


def _convert_goldens(node, targets, ownership_tag_str, golden_hashes):
    print("  Converting golden boxes to templates...")

    def _convert(t):
        # qm template refuses a VM holding snapshots, so delete-then-convert must stay in one worker.
        if SNAP_BASE in list_snapshots(node, t["vmid"]):
            delete_snapshot(node, t["vmid"], SNAP_BASE)
        # A conversion that loses the datastore lock fails while STILL leaving `template: 1` and the
        # volume un-renamed, so read the task's own result.
        with storage_queue_lock(node):
            for attempt in range(3):
                upid = proxmox_api("POST", f"/nodes/{node}/qemu/{t['vmid']}/template")["data"]
                if not upid:
                    break
                try:
                    wait_for_proxmox_task(node, upid)
                    break
                except RuntimeError as e:
                    if "lock file" not in str(e) or attempt == 2:
                        raise
                    print(f"    {t['vm_name']}: template conversion hit datastore lock "
                          f"contention — retrying (attempt {attempt + 2}/3)")
                    time.sleep(40 * (attempt + 1))
        _await_base_volumes(node, t["vmid"], t["box"]["name"])
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={"tags": ownership_tag_str})
        # The hash lands on the template description AND the comp's state, for the next deploy's reuse.
        if golden_hashes:
            write_template_hash(node, t["vmid"], golden_hashes[t["box"]["name"]],
                                extra=f"box={t['box']['name']}")
        with PRINT_LOCK:
            print(f"    {t['vm_name']} (vmid {t['vmid']}) is now a template")

    # Bound 4, not the default 8: full clones saturate the datastore.
    conv_results = run_concurrent(targets, _convert, max_workers=4)
    for _t, r in zip(targets, conv_results):
        if isinstance(r, Exception):
            raise r


def set_fedora_selinux_permissive(targets, ctx):
# Fedora's qemu-guest-agent domain is confined while SELinux enforces (dnf/systemctl denied),
# so make the golden permissive before it is sealed. The SSH channel lands unconfined, which
# is also why this runs before the agent-dependent setup. Debian/Ubuntu are left alone.
    fedora = [t for t in targets if "fedora" in t["box"]["template"].lower()]
    if not fedora:
        return
    print(f"  Setting SELinux permissive on {len(fedora)} Fedora golden box(es)...")
    cmd = ("sudo sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config && "
           "grep -q '^SELINUX=permissive' /etc/selinux/config && "
           "(sudo setenforce 0 2>/dev/null || true) && getenforce")

    def _one(t):
        r = ssh_via_gateway(ctx, t["ip"], cmd, timeout=120,
                            user=ctx.get("box_username", "ubuntu"))
        if r.returncode != 0:
            raise RuntimeError(
                f"could not set SELinux permissive on Fedora golden {t['vm_name']} "
                f"({t['ip']}): rc={r.returncode} "
                f"{(r.stderr or r.stdout or '').strip()[:160]}")
        with PRINT_LOCK:
            print(f"    {t['vm_name']}: /etc/selinux/config SELINUX=permissive "
                  f"(current: {(r.stdout or '').strip()})")

    results = run_concurrent(fedora, _one, max_workers=4)
    for _t, r in zip(fedora, results):
        if isinstance(r, Exception):
            raise r


def iis_management_features(comp_dir, box_name):
    """The Windows feature installs a box needs before its plan's IIS step runs."""
    try:
        services = json.loads((Path(comp_dir) / "box_services.json").read_text())
    except (OSError, ValueError):
        return ()
    entries = services.get(box_name) or []
    return ("Web-Mgmt-Tools",) if any("iis" in str(e).lower() for e in entries) else ()


def build_golden_set(node, teams, boxes, ctx, comp_dir, engine_vmid, box_password,
                     golden_config_path, key, scoring_user, scoring_ip, jobs=1,
                     golden_hashes=None, unbooted=frozenset(),
                     slot=0, anchor_identifier=None, run_id=None, coverage=None):
    targets = golden_targets(engine_vmid, teams, boxes, slot=slot,
                             anchor_identifier=anchor_identifier)
    own_set = ownership_tags(competition_name(comp_dir), run_id, GOLDEN_TAG)
    ownership_tag_str = ",".join(sorted(own_set))
    templates = _template_vmid_map(node)
    _golden_cfg_cache = []

    def _golden_cfg():
        if not _golden_cfg_cache:
            _golden_cfg_cache.append(
                json.loads(golden_config_path.read_text())["machines"])
        return _golden_cfg_cache[0]

    # Coverage keys are slot-qualified: every slot's stage config names its golden machine
    # identically, so one slot's replant would pop another slot's failure.
    def _coverage_machines(box_names):
        suffix = f"-slot{slot}" if slot else ""
        by_box = {m["name"][:-len("-golden")]: m for m in _golden_cfg()}
        return [{"name": f"{box}-golden{suffix}",
                 "configurations": by_box[box].get("configurations", [])}
                for box in box_names if box in by_box]

    missing = [t for t in targets if not _vm_exists(node, t["vmid"])]
    planted = [t for t in targets
               if _vm_exists(node, t["vmid"]) and not _is_template(node, t["vmid"])]
    converted = [t for t in targets if _is_template(node, t["vmid"])]
    if converted and len(converted) < len(targets):
        if golden_hashes is not None:
            mismatched = [t["box"]["name"] for t in converted
                          if stored_template_hash(node, t["vmid"])
                          != golden_hashes.get(t["box"]["name"])]
            if mismatched:
                raise RuntimeError(
                    "golden set is partially converted and these converted golden(s) do "
                    f"not match their expected hash: {', '.join(mismatched)} — destroy the "
                    "range and redeploy; a half-converted golden set has no clean resume "
                    "(templates can't be un-templated).")
        else:
            raise RuntimeError(
                "golden set is partially converted — destroy the range and redeploy; a "
                "half-converted golden set has no clean resume (templates can't be "
                "un-templated).")
    if len(converted) == len(targets):
        print(f"  Golden set already converted ({len(converted)} template(s)) — skipping "
              f"build (resume; M4 hash gate passed or rebuild already handled upstream)")
        # Kept templates carry an earlier run's ownership; re-stamp so teardown recognizes them.
        for t in converted:
            retag_ownership(node, t["vmid"], own_set)
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in targets), None)
        return {t["box"]["name"]: t["vmid"] for t in targets}

    done = {t["vmid"] for t in converted}
    for t in converted:
        retag_ownership(node, t["vmid"], own_set)
    cold = [t for t in targets if t["vmid"] not in done and t["box"]["name"] in unbooted]
    booted = [t for t in targets if t["vmid"] not in done and t["box"]["name"] not in unbooted]

    _build_unbooted_goldens(node, cold, templates, comp_dir, own_set, ownership_tag_str,
                            golden_hashes)
    if not booted:
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in targets), None)
        return {t["box"]["name"]: t["vmid"] for t in targets}
    missing = [t for t in missing if t in booted]
    planted = [t for t in planted if t in booted]
    targets_all, targets = targets, booted

    # A golden planted on an unchanged hash does not need it redone; trusted only while SNAP_BASE
    # exists, so a re-cloned golden is not mistaken for a done one.
    checkpoint_record = golden_plant_checkpoints(comp_dir, run_id) if golden_hashes else {}
    checkpointed = set()
    for t in planted:
        wanted = (golden_hashes or {}).get(t["box"]["name"])
        rec = checkpoint_record.get(t["box"]["name"]) or {}
        if (wanted and rec.get("hash") == wanted
                and SNAP_BASE in list_snapshots(node, t["vmid"])):
            checkpointed.add(t["box"]["name"])
            print(f"  Golden checkpoint: {t['box']['name']} already planted on this hash "
                  f"by run {rec.get('run') or '?'} — skipping its rollback and plant")
    work = [t for t in targets if t["box"]["name"] not in checkpointed]

    if missing:
        _clone_missing_goldens(node, missing, templates, comp_dir, ctx, box_password,
                               ownership_tag_str)

    work_vmids = {t["vmid"] for t in work}
    rolls = [t for t in planted
             if t["vmid"] in work_vmids and SNAP_BASE in list_snapshots(node, t["vmid"])]
    for t in rolls:
        print(f"  Golden re-entry: rolling {t['vm_name']} back to '{SNAP_BASE}' before re-planting...")
        rollback_snapshot(node, t["vmid"], SNAP_BASE)

    print("  Starting golden boxes...")
    for t in work:
        start_vm(node, t["vmid"])

    linux_targets = [t for t in work if not is_windows_template(t["box"]["template"])]
    windows_targets = [t for t in work if is_windows_template(t["box"]["template"])]

    def _boot_win(t):
        # 1800s: a fresh sysprep-specialize first boot can exceed the 900s default on a loaded node.
        bootstrap_windows_box(node, t["vmid"], t["ip"], t["gateway"], "8.8.8.8", box_password,
                              features=iis_management_features(comp_dir, t["box"]["name"]),
                              timeout=1800)

    win_results = run_concurrent(windows_targets, _boot_win, max_workers=4)
    for t, r in zip(windows_targets, win_results):
        if isinstance(r, Exception):
            raise r

    wait_for_boxes_ssh(ctx, work, timeout=300)
    wait_for_cloud_init(ctx, work, timeout=240)
    set_fedora_selinux_permissive(linux_targets, ctx)
    setup_ubuntu_auth(linux_targets, ctx)
    if linux_targets:
        print("  Expanding guest root filesystems (LVM layouts need pvresize+lvextend)...")
        expand_guest_root_disks(linux_targets, ctx)

    # Nakon authenticates by PASSWORD and an API clone of a base template has none set.
    box_username = ctx.get("box_username", "ubuntu")
    print(f"  Setting box passwords on golden Linux boxes (nakon authenticates as {box_username})...")

    def _set_box_password(t):
        r = ssh_via_gateway(ctx, t["ip"], f"echo '{box_username}:{box_password}' | sudo chpasswd",
                            timeout=120, user=box_username)
        if r.returncode != 0:
            raise RuntimeError(f"password reset failed on golden {t['ip']}: "
                               f"{(r.stderr or '').strip()[:120]}")

    pw_results = run_concurrent(linux_targets, _set_box_password, max_workers=4)
    for _t, r in zip(linux_targets, pw_results):
        if isinstance(r, Exception):
            raise r

    fix_dns_on_boxes(linux_targets, ctx)

    print("  Prepping apt on golden boxes (pre-plant)...")
    prep_apt_on_boxes(linux_targets, ctx)

    print(f"  Snapshotting golden boxes as '{SNAP_BASE}' (pre-plant rollback point)...")

    def _snap_base(t):
        take_snapshot(node, t["vmid"], SNAP_BASE,
                      description="tezcatlipoca golden: booted, networked, pre-plant")

    # run_concurrent joins the whole pool before returning, so every box's snapshot exists before
    # the plant; take_snapshot warns instead of raising, so an escaped exception must propagate.
    snap_results = run_concurrent(work, _snap_base, max_workers=4)
    for _t, r in zip(work, snap_results):
        if isinstance(r, Exception):
            raise r

    # With alpine_services, catalog service steps are EXPECTED to fail on Alpine (vulndb scripts are
    # apt/dnf/yum-only); the plant runs non-strict and the shim owns those services.
    alpine_shim = compfile_flag(comp_dir / "Compfile", "alpine_services")
    # An empty plan makes nakon report "no output from the remote plan" per machine.
    if not any(m.get("configurations") for m in _golden_cfg()):
        print("  Golden stage carries no plantable configurations — skipping the "
              "nakon plant; converting pristine goldens")
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in targets), None)
    elif not work:
        print("  Every golden is already planted — skipping the plant")
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in targets), None)
    else:
        bundle = build_nakon_bundle(golden_config_path)
        result = run_nakon(key, scoring_user, scoring_ip, bundle, golden_config_path,
                           only=[t["machine"] for t in work],
                           timeout=max(2400, 2400 * len(work)), strict=not alpine_shim,
                           jobs=jobs, run_tag="golden")
        if result.failed:
            if alpine_shim:
                print(f"  WARNING: golden plant had {len(result.failed)} FAILED step(s) — "
                      f"alpine_services shim owns those (apk/OpenRC)")
            else:
                raise RuntimeError(f"golden plant had {len(result.failed)} FAILED step(s) — strict mode requires green")
        # Record BEFORE the shim runs: an alpine-tolerated failure is a real gap on the golden disk,
        # and the record must survive a shim that then fails the deploy.
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in work), result)
        if alpine_shim:
            ensure_alpine_services(comp_dir, work, ctx)

    print("  Cleaning cloud-init state on golden Linux boxes (clones must re-init)...")

    def _cloud_init_clean(t):
        return ssh_via_gateway(ctx, t["ip"], "sudo cloud-init clean --logs --machine-id",
                               timeout=120, user=ctx.get("box_username", "ubuntu"))

    clean_results = run_concurrent(linux_targets, _cloud_init_clean, max_workers=4)
    for t, r in zip(linux_targets, clean_results):
        if isinstance(r, Exception):
            raise r
        if r.returncode != 0:
            print(f"    WARNING: cloud-init clean rc={r.returncode} on {t['ip']} "
                  f"— clones may inherit golden's machine-id")

    print("  Stopping golden boxes (clean shutdown before template conversion)...")
    for t in targets:
        stop_vm(node, t["vmid"])

    # Record per-box before conversion, so a later failure does not throw this box's plant away.
    if golden_hashes:
        for t in work:
            save_template_hashes(comp_dir, golden_planted={
                t["box"]["name"]: {"hash": golden_hashes[t["box"]["name"]],
                                   "run": run_id or ""}})

    _convert_goldens(node, targets, ownership_tag_str, golden_hashes)

    return {t["box"]["name"]: t["vmid"] for t in targets_all}


# Destroy AFTER the linked clones, which depend on the template's base disk.
OTHER_RUN = "another run of this competition"


def _destroy_template(node, vmid, expect_tags):
    """A VM carrying THIS competition's tag but another run's id is never touched — the caller
    reports it as blocking, not a foreign skip."""
    verdict, tags, vm = vm_occupancy(node, vmid, expect_tags)
    if verdict == "absent":
        return None
    if verdict == "unknown":
        return f"vmid {vmid} could not be classified (unreadable) — check by hand"
    if verdict in (OWNED, MARKER):
        destroy_vm_if_exists(node, vmid, expect_tags=expect_tags)
        return None
    comp = comp_name_from_tags(expect_tags or ())
    if verdict == MISSING and comp and f"comp-{comp}" in tags:
        return (f"{OTHER_RUN} holds vmid {vmid} "
                f"({vm.get('name') or '?'}, tags: {';'.join(sorted(tags))}) — not touched")
    return (f"vmid {vmid} is occupied by a foreign VM ({vm.get('name') or '?'}, "
            f"tags: {';'.join(sorted(tags)) or 'none'}) — skipped")


def destroy_golden_set(node, engine_vmid, num_box_types, expect_tags=None, slot=0):
    skipped = []
    for box_idx in range(num_box_types):
        message = _destroy_template(node, golden_vmid_for_slot(engine_vmid, slot, box_idx),
                                    expect_tags)
        if message:
            skipped.append(message)
            print(f"    WARNING: golden slot {box_idx}: {message}")
    return skipped


__all__ = ['build_golden_set', 'destroy_golden_set']
