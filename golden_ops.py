"""Golden-set build and template conversion (M3.1).

Plant once per box type on a "golden" VM, convert it to a template, then let Terraform
apply #2 link-clone every team from those templates. The heavy work (package installs,
services, non-disruptive misconfigs) lands once and rides the clone as bytes on disk.

Why the API and not Terraform: `qm template` conversion happens outside the VM resource's
lifecycle, so a Terraform-managed golden box would drift the moment it converts. This
module mirrors clone_ops' API path (clone, config, start, wait); Terraform only consumes
the finished templates as `clone { full = false }` sources via the golden_template_ids
tfvars map that deploy() persists.

Identity note: linked clones of a BOOTED golden share its machine SID across teams.
Harmless for members, but a new forest takes its domain SID from the promoted DC's
machine SID, so DCs cloned from one booted golden gave every team the same DomainSID
(winad-testrun 2026-09-25). Domain-controller box types therefore get an UNBOOTED
golden: a full clone of the sysprepped base converted straight to a template, so each
team's clone runs specialize itself (fresh SID). Their planted configs move to the
per-team repair stage (nakon_ops.generate_stage_configs), which still runs pre-domain.
"""

import json
import os
import time

from constants import GOLDEN_CLONE_TIMEOUT, GOLDEN_TAG, SNAP_BASE, ownership_tags
from engine_ops import ensure_nat_forwarding
from hardening_ops import (
    ensure_alpine_services,
    fix_dns_on_boxes,
    prep_apt_on_boxes,
    setup_ubuntu_auth,
)
from nakon_ops import build_nakon_bundle, run_nakon
from vm_ownership import full_clone_data
from range_ops import (
    clone_marker,
    gc_orphan_volumes,
    delete_snapshot,
    destroy_vm_if_exists,
    list_snapshots,
    proxmox_api,
    retag_ownership,
    rollback_snapshot,
    start_vm,
    stop_vm,
    take_snapshot,
    wait_for_proxmox_task,
)
from nodes_ops import golden_vmid_for_slot
from ssh_ops import ssh_via_gateway, wait_for_boxes_ssh, wait_for_cloud_init
from template_ops import (
    golden_plant_checkpoints,
    save_template_hashes,
    stored_template_hash,
    write_template_hash,
)
from utils import PRINT_LOCK, compfile_flag, run_concurrent
from windows_ops import bootstrap_windows_box, is_windows_template
# Names that moved to sibling modules stay importable from here (this module is the
# public path; see docs/internals.md 'golden_ops split').
from golden_target_ops import (
    _quote_sshkeys,
    golden_vmid_for,
    golden_ip_for,
    _is_template,
    _vm_exists,
    _template_vmid_map,
    unbooted_golden_boxes,
    golden_targets,
)
from golden_smoke_ops import (
    BOOT_SMOKE_PASS,
    BOOT_SMOKE_UNBOOTABLE,
    BOOT_SMOKE_UNVERIFIED,
    GOLDEN_BOOT_SMOKE_TAG,
    GOLDEN_BOOT_SMOKE_TIMEOUT,
    GOLDEN_BOOT_SMOKE_WIN_TIMEOUT,
    GOLDEN_BOOT_SMOKE_POLL,
    _smoke_vmid,
    _destroy_smoke_clone,
    _probe_boot,
    _boot_smoke_error,
    golden_boot_smoke,
)
from golden_disk_ops import (
    _SIZE_TO_GB,
    _root_disk_gb,
    ensure_golden_disk_size,
    expand_guest_root_disks,
)


def _build_unbooted_goldens(node, cold, templates, comp_dir, own_set, ownership_tag_str,
                           golden_hashes):
    """Unbooted (DC) goldens: full clone of the sysprepped base converted straight to a
    template — never booted, so each team clone specializes its own SID."""
    for t in cold:
        # Any plain VM here is a dead attempt that may have booted (and specialized) —
        # never reuse it; the whole point of this slot is a generalized disk.
        # Adoption exception (live-found 2026-09-30, same shape as the engine-template
        # leftover): a clone task that outlived its 1800s task-wait completes AFTER
        # the driver raised, leaving the VM with only the BASE image's inherited tags
        # — the strict guard would refuse it forever. A not-yet-converted VM on the
        # reserved slot carrying our clone-marker description is ours; adopt it loudly.
        try:
            cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{t['vmid']}/config")["data"]
            desc = str(cfg.get("description") or "")
            raw_tags = str(cfg.get("tags") or "")
            tags = {x.strip() for x in raw_tags.replace(";", ",").split(",") if x.strip()}
            if (cfg.get("name") == t["vm_name"] and clone_marker(comp_dir.name) in desc
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
                           data=full_clone_data(t["vmid"], t["vm_name"], comp_dir.name))["data"]
        wait_for_proxmox_task(node, upid, timeout=GOLDEN_CLONE_TIMEOUT)
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={
            "net0": f"virtio,bridge={t['bridge']}", "tags": ownership_tag_str})
        proxmox_api("POST", f"/nodes/{node}/qemu/{t['vmid']}/template")
        if golden_hashes:
            write_template_hash(node, t["vmid"], golden_hashes[t["box"]["name"]],
                                extra=f"box={t['box']['name']} unbooted")
        print(f"    {t['vm_name']} (vmid {t['vmid']}) is now an UNBOOTED template "
              f"(generalized — each team's clone specializes its own SID)")


def _clone_missing_goldens(node, missing, templates, comp_dir, ctx, box_password,
                          ownership_tag_str):
    """Full-clone each missing golden from its base template, adopting/clearing an
    interrupted clone only on proof of ownership (our clone marker)."""
    print(f"  Cloning {len(missing)} golden box(es) from base templates...")
    existing_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
    for t in missing:
        src = templates.get(t["box"]["template"])
        if src is None:
            raise RuntimeError(f"no stopped template named '{t['box']['template']}' on the node")
        if t["vmid"] in existing_vmids:
            locked_cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{t['vmid']}/config")["data"]
            if not locked_cfg.get("lock"):
                continue
            # An interrupted clone: half-copied disk, never usable — start over.
            # Ownership proof: only THIS competition's clone marker in the
            # description adopts it; expect_tags=set() used to destroy ANY VM
            # found here, foreign ones included.
            if clone_marker(comp_dir.name) not in str(locked_cfg.get("description") or ""):
                raise RuntimeError(
                    f"refusing to destroy locked vmid {t['vmid']} on reserved golden "
                    f"slot '{t['vm_name']}' — no clone marker in its description, "
                    f"not provably ours")
            destroy_vm_if_exists(node, t["vmid"], expect_tags=None)
        else:
            gc_orphan_volumes(node, t["vmid"])
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone",
                           data=full_clone_data(t["vmid"], t["vm_name"], comp_dir.name))["data"]
        wait_for_proxmox_task(node, upid, timeout=GOLDEN_CLONE_TIMEOUT)
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={
            "ipconfig0": f"ip={t['ip']}/24,gw={t['gateway']}",
            "net0": f"virtio,bridge={t['bridge']}",
            "tags": ownership_tag_str,
            # cloud-init identity for the golden box's first boot (the base template
            # bakes no password — terraform's user_account normally supplies it):
            "ciuser": ctx.get("box_username", "ubuntu"),
            "cipassword": box_password,
            "sshkeys": _quote_sshkeys(os.environ["TF_VAR_ssh_public_key"]),
        })
        ensure_golden_disk_size(node, t["vmid"], t["box"].get("disk_gb"))
        print(f"    {t['box']['template']} -> {t['vm_name']} (vmid {t['vmid']})")


def _base_volumes_missing(node, vmid):
    """Disk volumes that still lack the base- prefix. The template conversion renames
    volumes to base-<vmid>-disk-N; linked clones only work from those (live-found
    2026-10-04: a golden half-converted by an interrupted POST kept template=1 on its
    plain vm- volume, and every team clone of it died with HTTP 500 "Linked clone
    feature is not supported")."""
    cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    # Disk ifaces only (ide carries the cloudinit drive; its name differs) — a volume
    # string is "<storage>:<name>,<opts>"; the NAME is what the conversion renames.
    volids = [str(v) for k, v in cfg.items()
              if k.startswith(("scsi", "sata", "virtio")) and isinstance(v, str) and ":" in v]
    return [v for v in volids if not v.split(":", 1)[1].split(",")[0].startswith("base-")]


def _convert_goldens(node, targets, ownership_tag_str, golden_hashes):
    """Stop-barrier already passed: convert every target golden to a template (parallel,
    bound 4). Delete-then-convert stays inside one worker."""
    print("  Converting golden boxes to templates...")

    def _convert(t):
        # qm template refuses a VM holding snapshots — and the tz-base rollback guard's
        # purpose ends with the plant: a converted set is fatal on re-entry by design.
        # Delete-then-convert stays INSIDE one worker, so a box's snapshot is always
        # gone before the POST /template that needs it gone.
        if SNAP_BASE in list_snapshots(node, t["vmid"]):
            delete_snapshot(node, t["vmid"], SNAP_BASE)
        proxmox_api("POST", f"/nodes/{node}/qemu/{t['vmid']}/template")["data"]
        # The POST returns no task to wait on and the rename can lag or lose the race
        # against an interrupted session; a half-converted golden passes every hash
        # gate and then breaks apply #2 confusingly. Verify the rename landed.
        for _ in range(10):
            missing = _base_volumes_missing(node, t["vmid"])
            if not missing:
                break
            time.sleep(2)
        if missing:
            raise RuntimeError(
                f"golden '{t['box']['name']}' (vmid {t['vmid']}) converted but its disk "
                f"volume(s) {missing} never became base- volumes — linked clones would "
                f"fail with 'Linked clone feature is not supported'. Destroy this golden "
                f"and re-run phase 4 so it is rebuilt from scratch.")
        proxmox_api("PUT", f"/nodes/{node}/qemu/{t['vmid']}/config", data={"tags": ownership_tag_str})
        # M4: the hash lands on the template (description) AND in the comp's state —
        # reuse/rebuild decisions read it back at the next deploy's hash gate.
        if golden_hashes:
            write_template_hash(node, t["vmid"], golden_hashes[t["box"]["name"]],
                                extra=f"box={t['box']['name']}")
        with PRINT_LOCK:
            print(f"    {t['vm_name']} (vmid {t['vmid']}) is now a template")

    # Parallel since W11 (2026-10-01): 4-5 API calls plus a snapshot-delete task wait
    # per box (the converting POST itself returns no task to wait on), which is ~5-10s
    # per box, so an 8-box-type build paid ~30-60s serially. Bound 4, NOT
    # MAX_CONCURRENCY's 8: each unit drives Proxmox tasks and 4 is the validated
    # per-box VM-work bound (deploy.py's snapshot/Windows-bootstrap pools, jump_ops).
    #
    # The `if boot_smoke:` block ABOVE is a hard phase barrier: it runs first and
    # raises on any non-PASS verdict, so a golden that failed its boot smoke can never
    # reach POST /template no matter how this pool schedules. The clone loops (cold
    # path, and `missing`) are deliberately NOT parallel: full clones are
    # crash-consistent copies and saturate the datastore (see the module note on the
    # cold path and deploy.py's -parallelism=1).
    conv_results = run_concurrent(targets, _convert, max_workers=4)
    for _t, r in zip(targets, conv_results):
        if isinstance(r, Exception):
            raise r


def build_golden_set(node, teams, boxes, ctx, comp_dir, engine_vmid, box_password,
                     golden_config_path, key, scoring_user, scoring_ip, jobs=1,
                     golden_hashes=None, unbooted=frozenset(),
                     slot=0, anchor_identifier=None, run_id=None, coverage=None):
    """Clone, plant (strict), and convert the golden set. Returns {box_name: vmid}.

    Resume routing: every golden vmid already a template -> skip (the build is done);
    golden vmids exist as plain VMs -> a previous attempt died mid-build, so roll back
    the ones tz-base marks as planted and re-plant; missing ones are cloned fresh.
    A PARTIALLY converted set is fine when every converted vmid's description hash
    matches its expected hash (M4 selective rebuild / added box type); without
    expected hashes, the historical no-clean-resume refusal stands.

    coverage (optional) — callable(machines, result) recording the plant-coverage
    verdict: `machines` are {"name", "configurations"} dicts keyed for verify's golden
    mapping ('{box}-golden', slot-qualified on satellites), `result` the NakonResult
    when the plant ran (clean OR alpine_services-tolerated failures), None on every
    skip path (reuse / cold / pristine / checkpointed)."""
    targets = golden_targets(engine_vmid, teams, boxes, slot=slot,
                             anchor_identifier=anchor_identifier)
    # Full ownership set (comp tag + per-deploy run tag): stamped on everything this
    # build creates and required before any destroy here touches a VM.
    own_set = ownership_tags(comp_dir.name, run_id, GOLDEN_TAG)
    ownership_tag_str = ",".join(sorted(own_set))
    templates = _template_vmid_map(node)
    # Loaded lazily but only once: the coverage skip paths and the plant section both
    # need the stage machines, while the all-converted resume path must work without
    # ever reading the config (same contract as before the coverage hook existed).
    _golden_cfg_cache = []

    def _golden_cfg():
        if not _golden_cfg_cache:
            _golden_cfg_cache.append(
                json.loads(golden_config_path.read_text())["machines"])
        return _golden_cfg_cache[0]

    def _coverage_machines(box_names):
        """Coverage keys for the named box types: '{box}-golden' on slot 0,
        '{box}-golden-slot{N}' on satellite slots — every slot's stage config names
        its golden machine identically, so the key must be slot-qualified or one
        slot's clean replant would pop another slot's recorded failure."""
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
        # Kept templates carry an earlier run's ownership (M4 reuse across runs) —
        # re-stamp so teardown/preflight recognize them.
        for t in converted:
            retag_ownership(node, t["vmid"], own_set)
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in targets), None)
        return {t["box"]["name"]: t["vmid"] for t in targets}

    # Only unconverted slots are worked on: a selective rebuild (one box type's hash
    # changed) must not start, plant, or re-convert the matching templates it keeps.
    done = {t["vmid"] for t in converted}
    for t in converted:
        retag_ownership(node, t["vmid"], own_set)
    cold = [t for t in targets if t["vmid"] not in done and t["box"]["name"] in unbooted]
    booted = [t for t in targets if t["vmid"] not in done and t["box"]["name"] not in unbooted]

    _build_unbooted_goldens(node, cold, templates, comp_dir, own_set, ownership_tag_str,
                            golden_hashes)
    if not booted:
        # All-cold slot (every box type unbooted): nothing here is ever nakon-planted,
        # and unbooted box types don't appear in the stage config at all — the clean
        # marker is the whole record for this slot.
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in targets), None)
        return {t["box"]["name"]: t["vmid"] for t in targets}
    missing = [t for t in missing if t in booted]
    planted = [t for t in planted if t in booted]
    targets_all, targets = targets, booted

    # Per-golden checkpoint. A re-entry used to roll EVERY planted golden back to
    # tz-base and re-plant the whole set; measured across the cde-2026 attempts,
    # `Golden re-entry: rolling golden-<name> back to 'tz-base'` appears 33 times — 11
    # each for web01, ftp01 and db01 — over 10 runs, while only ftp01 was ever the
    # problem. A golden that already passed its plant AND its boot smoke on an
    # unchanged hash does not need either redone.
    #
    # The conversion barrier is deliberately NOT relaxed: every golden is still stopped
    # and every *pending* one still smoked before any POST /template, so one failed
    # smoke still blocks the whole conversion (tests/test_parallel_golden.py pins that).
    # A checkpoint is only trusted while SNAP_BASE still exists on the box — a fresh
    # clone has no such snapshot, so a re-cloned golden is never mistaken for a done one.
    checkpoint_record = golden_plant_checkpoints(comp_dir) if golden_hashes else {}
    checkpointed = set()
    for t in planted:
        wanted = (golden_hashes or {}).get(t["box"]["name"])
        if (wanted and checkpoint_record.get(t["box"]["name"]) == wanted
                and SNAP_BASE in list_snapshots(node, t["vmid"])):
            checkpointed.add(t["box"]["name"])
    if checkpointed:
        print(f"  Golden checkpoint: {', '.join(sorted(checkpointed))} already planted and "
              f"smoke-passed on this hash — skipping their rollback, plant and smoke")
    # Everything below that does WORK (start, wait, prepare, snapshot, plant, smoke,
    # cloud-init clean) is scoped to `work`; the stop-and-convert barrier still covers
    # all of `targets`.
    work = [t for t in targets if t["box"]["name"] not in checkpointed]

    # Boot smoke gate (Compfile `golden_boot_smoke`, default ON): the ONLY way to skip
    # verifying the boot-hostile-config invariant is to say so deliberately — and then
    # the warning below is printed, because the alternative is discovering the breakage
    # on every team clone one phase later (2026-09-24 systemd-system-masked).
    boot_smoke = bool(compfile_flag(comp_dir / "Compfile", "golden_boot_smoke", 1))
    if not boot_smoke:
        print("  WARNING: golden_boot_smoke is OFF in the Compfile — the boot-hostile "
              "config invariant is UNVERIFIED this run. A golden that cannot boot will be "
              "converted to a template and every linked team clone will fail to boot "
              "(2026-09-24 systemd-system-masked). Re-enable `golden_boot_smoke 1` to "
              "verify before conversion.")

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
        # 1800s: a fresh sysprep-specialize first boot can exceed the 900s default
        # outright on a loaded node (live-found 2026-09-30: two consecutive golden
        # rebuilds died at exactly 900s with a second deploy saturating the host).
        bootstrap_windows_box(node, t["vmid"], t["ip"], t["gateway"], "8.8.8.8", box_password,
                              timeout=1800)

    win_results = run_concurrent(windows_targets, _boot_win, max_workers=4)
    for t, r in zip(windows_targets, win_results):
        if isinstance(r, Exception):
            raise r

    wait_for_boxes_ssh(ctx, work, timeout=300)
    wait_for_cloud_init(ctx, work, timeout=240)
    setup_ubuntu_auth(linux_targets, ctx)
    if linux_targets:
        print("  Expanding guest root filesystems (LVM layouts need pvresize+lvextend)...")
        expand_guest_root_disks(linux_targets, ctx)

    # Nakon authenticates to the golden boxes by PASSWORD, and an API clone of a base
    # template has no password set (terraform's user_account normally supplies it at
    # team-box creation). Reset it over the key-auth SSH channel before the plant.
    box_username = ctx.get("box_username", "ubuntu")
    print(f"  Setting box passwords on golden Linux boxes (nakon authenticates as {box_username})...")

    def _set_box_password(t):
        r = ssh_via_gateway(ctx, t["ip"], f"echo '{box_username}:{box_password}' | sudo chpasswd",
                            timeout=30, user=box_username)
        if r.returncode != 0:
            raise RuntimeError(f"password reset failed on golden {t['ip']}: "
                               f"{(r.stderr or '').strip()[:120]}")

    # Parallel since W11 (2026-10-01): one independent SSH round trip per box, so an
    # 8-box-type build paid ~8-16s of pure waiting for no reason (each box's reset is
    # independent of every other box's). Same bound and same zip-and-raise aggregation
    # as the Windows bootstrap above: a single failure still aborts the build with the
    # identical message.
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

    # Parallel since W11 (2026-10-01): deploy.py's tz-base/tz-ready passes have run this
    # exact op through run_concurrent(..., max_workers=4) since M2.1, so the serial loop
    # here was an inconsistency as much as a slow path. The per-box snapshot wall,
    # measured over 13 competitions (.deploy-timings.jsonl: 460 successful records, 16
    # sub-second warn-path records excluded) is median 7.4s / mean 12.1s / p95 33.8s /
    # max 103.3s — bench-parallel-2026-09-24 phase 4 alone was 12 snapshots / 135s — so
    # an 8-box-type golden build paid ~60-100s of serial waiting. Bound 4, the same
    # per-box bound deploy.py validated. run_concurrent joins the whole pool before it
    # returns, so every box's snapshot still exists before the plant, the smoke gate,
    # and the conversion that follow: the rollback-guard ordering is unchanged.
    snap_results = run_concurrent(work, _snap_base, max_workers=4)
    # take_snapshot warns and returns False instead of raising, so the serial semantics
    # are warn-and-continue; anything that still escaped it must keep propagating.
    for _t, r in zip(work, snap_results):
        if isinstance(r, Exception):
            raise r

    ensure_nat_forwarding(ctx)

    # With the alpine_services knob, catalog service steps are EXPECTED to fail on
    # Alpine (vulndb scripts are apt/dnf/yum-only) — the plant runs non-strict and
    # ensure_alpine_services owns those services, with its own hard pass/fail.
    alpine_shim = compfile_flag(comp_dir / "Compfile", "alpine_services")
    # Score-only lineups carry no plantable golden configurations — the golden is a
    # pristine disk by design. An empty plan makes nakon report "no output from the
    # remote plan" per machine, so the plant is skipped outright (live-found
    # 2026-09-30 multinode-spread).
    if not any(m.get("configurations") for m in _golden_cfg()):
        print("  Golden stage carries no plantable configurations — skipping the "
              "nakon plant; converting pristine goldens")
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in targets), None)
    elif not work:
        print("  Every golden is already planted and smoke-passed — skipping the plant")
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
        # Record BEFORE the shim runs: an alpine-tolerated failure is a real gap on
        # the golden disk (verify maps '{box}-golden' onto every team clone), and if
        # ensure_alpine_services then fails the deploy, the record must already be
        # on disk. A strict failure raised above and never reaches this line.
        if coverage:
            coverage(_coverage_machines(t["box"]["name"] for t in work), result)
        if alpine_shim:
            ensure_alpine_services(comp_dir, work, ctx)

    print("  Cleaning cloud-init state on golden Linux boxes (clones must re-init)...")

    def _cloud_init_clean(t):
        return ssh_via_gateway(ctx, t["ip"], "sudo cloud-init clean --logs --machine-id",
                               timeout=30, user=ctx.get("box_username", "ubuntu"))

    # Parallel since W11 (2026-10-01): same ~1-2s independent SSH round trip as the
    # password reset. The serial loop never let a non-zero rc abort — it warned — so
    # the warnings are printed from the collected results in box order, keeping the log
    # deterministic; a transport exception still propagates exactly as it did serially.
    clean_results = run_concurrent(linux_targets, _cloud_init_clean, max_workers=4)
    for t, r in zip(linux_targets, clean_results):
        if isinstance(r, Exception):
            raise r
        if r.returncode != 0:
            print(f"    WARNING: cloud-init clean rc={r.returncode} on {t['ip']} "
                  f"— clones may inherit golden's machine-id")

    # The golden is stopped BEFORE the smoke check: PVE only linked-clones templates, so
    # the throwaway is a full clone, and a full clone of a running VM would be a
    # crash-consistent copy rather than the exact bytes qm template will seal. Stopping
    # does not mutate the disk (no reboot, no cloud-init re-init) — it is the same stop
    # the conversion below needs, just moved up.
    print("  Stopping golden boxes (clean shutdown before the boot smoke check and "
          "template conversion)...")
    for t in targets:
        stop_vm(node, t["vmid"])

    if boot_smoke:
        planted_configs = sorted(
            c if isinstance(c, str) else c.get("name", "")
            for m in _golden_cfg() for c in (m.get("configurations") or []))
        # Only the boxes this run planted need re-smoking: a checkpointed golden's disk
        # has not changed since it passed, and it was never started this run. A failure
        # here still raises BEFORE the conversion below, so the barrier holds.
        for t in work:
            golden_boot_smoke(node, t, ctx, comp_dir, planted_configs=planted_configs,
                              run_id=run_id)
            # Record per-box, immediately: if the NEXT box's smoke fails, this one's
            # work must not be thrown away by the next re-entry.
            if golden_hashes:
                save_template_hashes(comp_dir, golden_planted={
                    t["box"]["name"]: golden_hashes[t["box"]["name"]]})

    _convert_goldens(node, targets, ownership_tag_str, golden_hashes)

    return {t["box"]["name"]: t["vmid"] for t in targets_all}


def destroy_golden_set(node, engine_vmid, num_box_types, expect_tags=None, slot=0):
    """Tear the golden templates down. Destroy order: AFTER the linked clones (they
    depend on the template's base disk). Slot-aware: each node's copy of the golden
    set dies on its own host. A slot held by a FOREIGN VM (ownership guard refusal)
    is skipped with a loud warning — one squatted vmid must not strand the rest of
    the teardown (loadtest-2026-09-30: a satellite template squatting golden slot
    1333 aborted the whole pass, leaving everything else half-destroyed)."""
    skipped = []
    for box_idx in range(num_box_types):
        try:
            destroy_vm_if_exists(node, golden_vmid_for_slot(engine_vmid, slot, box_idx),
                                 expect_tags=expect_tags)
        except RuntimeError as e:
            skipped.append(str(e))
            print(f"    WARNING: golden slot {box_idx} is FOREIGN — skipping it and "
                  f"continuing: {e}")
    if skipped:
        print(f"  {len(skipped)} golden slot(s) skipped as foreign — they belong to "
              f"another deployment; inspect them manually.")


__all__ = [
    'build_golden_set',
    'destroy_golden_set',
    '_quote_sshkeys',
    'golden_vmid_for',
    'golden_ip_for',
    '_is_template',
    '_vm_exists',
    '_template_vmid_map',
    'unbooted_golden_boxes',
    'golden_targets',
    'BOOT_SMOKE_PASS',
    'BOOT_SMOKE_UNBOOTABLE',
    'BOOT_SMOKE_UNVERIFIED',
    'GOLDEN_BOOT_SMOKE_TAG',
    'GOLDEN_BOOT_SMOKE_TIMEOUT',
    'GOLDEN_BOOT_SMOKE_WIN_TIMEOUT',
    'GOLDEN_BOOT_SMOKE_POLL',
    '_smoke_vmid',
    '_destroy_smoke_clone',
    '_probe_boot',
    '_boot_smoke_error',
    'golden_boot_smoke',
    '_SIZE_TO_GB',
    '_root_disk_gb',
    'ensure_golden_disk_size',
    'expand_guest_root_disks',
]
