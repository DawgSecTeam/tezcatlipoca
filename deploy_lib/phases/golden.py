"""Phase 4: plant the golden set, then terraform apply #2 (every team a linked clone)."""

from functools import partial

from constants import SNAP_BASE
from golden_ops import _template_vmid_map, build_golden_set
from hardening_ops import fix_dns_on_boxes, prep_apt_on_boxes, setup_ubuntu_auth
from nakon_ops import generate_slot_golden_config
from nodes_ops import record_of
from ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
from template_ops import load_template_hashes, save_template_hashes
from timing import timed
from utils import is_unmanaged, run_concurrent, run_terraform
from windows_ops import bootstrap_windows_box

from deploy_lib.coverage import record_coverage
from deploy_lib.golden_plan import golden_rebuild_gate
from deploy_lib.phases._snapshots import snap_base
from deploy_lib.phases._terraform import terraform_env_and_cwd, write_tfvars


def boot_win(ctx, t):
    """Bootstrap one Windows box (apply #2 re-created it as a fresh clone)."""
    print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
    gw = f"192.168.{t['identifier']}.1"
    with timed(ctx.comp_dir, 4, "bootstrap_windows", t["ip"]):
        bootstrap_windows_box(t.get("node", ctx.node), t["vmid"], t["ip"], gw,
                              "8.8.8.8", ctx.box_password)



def golden_coverage_recorder(ctx):
    """The `coverage` callback build_golden_set threads through its plant.

    The golden plant's coverage verdict, recorded like a post-clone stage's. result is
    None on every skip path (M4 reuse / checkpointed / pristine / cold): the disk is then
    proven by hash, not by a fresh plant, so guarantee the record exists but never erase a
    previously recorded failure. A real result records through record_coverage —
    including alpine_services-tolerated failures, which are a genuine gap on the golden
    disk and must reach verify's gate."""
    def _golden_coverage(machines, result):
        if result is None or not getattr(result, "machines", None):
            ctx.state.setdefault("plant_coverage_failed", {})
        else:
            record_coverage(ctx.state, machines, result)
            if result.failed:
                ctx.state["nakon_failed_steps"] = [
                    f"golden: {line}" for line in result.failed[:20]]
        ctx.save_state()
    return _golden_coverage


def build_goldens(ctx, stored):
    """Plant + convert the golden set on the engine node and on every satellite.

    Returns (golden_ids, golden_ids_by_slot): slot 0 is the engine node's map, satellites
    get their own (same planted content, built ON each satellite from its own templates).
    M4 hash gate: a converted golden whose stored hash differs is rebuilt — unless the
    competition is frozen, in which case config drift hard-fails (frozen_gate) and
    code-only drift reuses the template with a warning. Matching templates reach
    build_golden_set and short-circuit the build."""
    _golden_coverage = golden_coverage_recorder(ctx)

    # Slot-0 goldens only exist when the engine node actually hosts teams —
    # an all-satellite spread leaves the engine with no local bridges to
    # anchor them on (their vmbr<id> lives on the satellite's host).
    engine_has_teams = bool(
        ctx.placement is None
        or ctx.placement["team_nodes"] and any(
            n == ctx.placement["engine_node"]
            for n in ctx.placement["team_nodes"].values()))
    golden_ids = {}
    if engine_has_teams:
        golden_rebuild_gate(ctx.comp_dir, ctx.node, 0, ctx.boxes, ctx.engine_vmid,
                            stored, ctx.golden_hashes, ctx.golden_inputs, ctx.comp_name,
                            run_tag=ctx.reclaim_tag)
        golden_ids = build_golden_set(ctx.node, ctx.teams, ctx.boxes, ctx.tf_ctx, ctx.comp_dir,
                                      ctx.engine_vmid, ctx.box_password,
                                      ctx.golden_config_path, ctx.ssh_key, ctx.scoring_user,
                                      ctx.scoring_ip, jobs=ctx.nakon_jobs,
                                      golden_hashes=ctx.golden_hashes, unbooted=ctx.unbooted,
                                      run_id=ctx.run_id, coverage=_golden_coverage)
    golden_ids_by_slot = {0: golden_ids}
    if ctx.placement and ctx.placement["satellites"]:
        # Satellite goldens: identical planted content (same hashes), built ON
        # each satellite from its own templates, at the slot's anchor subnet —
        # linked clones can't cross hosts on separate storages. The plant rides
        # the engine's gateway path; the jump routes + SNATs it there.
        for sat in ctx.placement["satellites"]:
            sat_rec = record_of(ctx.placement, sat["name"])
            slot = sat["slot"]
            anchor = sat["anchor_identifier"]
            golden_rebuild_gate(ctx.comp_dir, sat_rec.node, slot, ctx.boxes,
                                ctx.engine_vmid, stored, ctx.golden_hashes,
                                ctx.golden_inputs, ctx.comp_name,
                                run_tag=ctx.reclaim_tag)
            slot_config = generate_slot_golden_config(ctx.comp_dir, ctx.boxes,
                                                      ctx.unbooted, anchor, slot)
            print(f"  Golden set on satellite '{sat['name']}' (slot {slot}, "
                  f"anchor 192.168.{anchor}.0/24)...")
            with timed(ctx.comp_dir, 4, "golden_build", f"sat{slot}"):
                golden_ids_by_slot[slot] = build_golden_set(
                    sat_rec.node, ctx.teams, ctx.boxes, ctx.tf_ctx, ctx.comp_dir, ctx.engine_vmid,
                    ctx.box_password, slot_config, ctx.ssh_key, ctx.scoring_user, ctx.scoring_ip,
                    jobs=ctx.nakon_jobs, golden_hashes=ctx.golden_hashes,
                    unbooted=ctx.unbooted, slot=slot, anchor_identifier=anchor,
                    run_id=ctx.run_id, coverage=_golden_coverage)
    return golden_ids, golden_ids_by_slot


def record_golden_set(ctx, golden_ids, golden_ids_by_slot):
    """Persist the built goldens (state + the per-competition template-hash file)."""
    ctx.state["golden_template_ids"] = golden_ids
    ctx.state["golden_ids_by_slot"] = {str(k): v for k, v in golden_ids_by_slot.items()}
    ctx.state["golden_hashes"] = ctx.golden_hashes
    save_template_hashes(ctx.comp_dir, golden={
        name: {"hash": ctx.golden_hashes[name], "inputs": ctx.golden_inputs[name]}
        for name in ctx.golden_hashes})
    ctx.save_state()


def apply_team_boxes(ctx, golden_ids, golden_ids_by_slot):
    """Flip build_team_boxes on and run terraform apply #2: every team box, linked-cloned.

    Positional per box: two box types may share one base template, so a name-keyed
    map would silently cross-wire golden disks (live-found 2026-09-24: web01
    clones came from golden-db01's disk).
    Length must equal boxes_per_team (positional). Unmanaged boxes (pfSense) have
    no golden — terraform's team_box unmanaged branch clones them from their own
    base template instead, so their slot here carries that base template's vmid
    — ON THE TEAM'S OWN NODE for satellite slots (their clone must resolve
    locally)."""
    _tmap = _template_vmid_map(ctx.node)
    ctx.tfvars["golden_template_ids"] = [
        int(_tmap[b["template"]]) if is_unmanaged(b)
        # 0 placeholder when the engine hosts no teams (all-satellite spread):
        # the slot-0 team_box for_each is empty then, so nothing reads it.
        else int(golden_ids.get(b["name"]) or 0)
        for b in ctx.boxes
    ]
    if ctx.placement and ctx.placement["satellites"]:
        by_slot_ids = {str(k): v for k, v in golden_ids_by_slot.items()}
        ctx.tfvars["golden_template_ids_by_slot"] = {}
        for sat in ctx.placement["satellites"]:
            slot = sat["slot"]
            sat_node = record_of(ctx.placement, sat["name"]).node
            stmap = _template_vmid_map(sat_node)
            slot_ids = by_slot_ids.get(str(slot)) or {}
            ctx.tfvars["golden_template_ids_by_slot"][str(slot)] = [
                int(stmap[b["template"]]) if is_unmanaged(b) else int(slot_ids[b["name"]])
                for b in ctx.boxes
            ]
    ctx.tfvars["build_team_boxes"] = True
    write_tfvars(ctx)
    tf_env, tf_cwd = terraform_env_and_cwd(ctx)
    # Linked clones are seconds each (no bulk disk copy); the budget is for the
    # cloud-init-adjacent API waits bpg does per box, not for storage.
    apply_timeout = 600 + 300 * len(ctx.all_targets)
    with timed(ctx.comp_dir, 4, "terraform_apply_teams"):
        run_terraform(["apply", "-auto-approve", "-parallelism=1"],
                      cwd=tf_cwd, env=tf_env, timeout=apply_timeout)


def bootstrap_clones(ctx):
    """Bring the fresh clones to a plantable state and take the pre-sweep restore point."""
    # Apply #2 (re-)created the team boxes: any surviving .postclone-swept
    # marker describes the OLD clones, and a phase-5 resume would skip the
    # repair sweep/credlist/shim on the fresh ones (hit twice on
    # shakedown-5x4; the workaround was deleting the marker by hand).
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)

    win_results = run_concurrent(ctx.windows_targets, partial(boot_win, ctx), max_workers=4)
    for t, r in zip(ctx.windows_targets, win_results):
        if isinstance(r, Exception):
            raise r

    # Waits and the pre-sweep snapshot cover the MANAGED boxes only: an unmanaged
    # appliance (pfSense) accepts no SSH and runs no cloud-init, and the in-path
    # firewall is not even routed until phase 5's cutover — its SNAP_BASE restore
    # point is taken there, once it actually carries the team's gateway.
    with timed(ctx.comp_dir, 4, "wait_boxes_ssh"):
        wait_for_boxes_ssh(ctx.tf_ctx, ctx.managed_targets, timeout=300)
    with timed(ctx.comp_dir, 4, "wait_cloud_init"):
        wait_for_cloud_init(ctx.tf_ctx, ctx.managed_targets, timeout=240)
    with timed(ctx.comp_dir, 4, "setup_auth"):
        setup_ubuntu_auth(ctx.linux_targets, ctx.tf_ctx)
    with timed(ctx.comp_dir, 4, "fix_dns"):
        fix_dns_on_boxes(ctx.linux_targets, ctx.tf_ctx)
    with timed(ctx.comp_dir, 4, "prep_apt"):
        prep_apt_on_boxes(ctx.linux_targets, ctx.tf_ctx, use_proxy=ctx.apt_cache)

    print(f"  Snapshotting all managed boxes as '{SNAP_BASE}' (pre-sweep restore point)...")
    run_concurrent(ctx.managed_targets, partial(snap_base, ctx), max_workers=4)


def phase4_golden_set(ctx):
    """[4/8] Plant the golden set, then terraform apply #2 to build every team box.

    Two halves on purpose: the goldens must exist as templates before apply #2
    flips build_team_boxes on, and both are timed under phase 4 so the timing
    summary shows the plant and the linked-clone burst separately. The [4/8] and
    [4b/8] banners stay distinct because operators (and incident logs) key on
    them."""
    if ctx.from_phase > 4:
        print("[4/8] Skipped (resume).")
        return

    print("[4/8] Building the golden set (plant once per box type, convert to template)...")
    stored = load_template_hashes(ctx.comp_dir)
    golden_ids, golden_ids_by_slot = build_goldens(ctx, stored)
    record_golden_set(ctx, golden_ids, golden_ids_by_slot)

    print("[4b/8] Terraform apply #2: every team as a linked clone of the golden set...")
    apply_team_boxes(ctx, golden_ids, golden_ids_by_slot)
    bootstrap_clones(ctx)
