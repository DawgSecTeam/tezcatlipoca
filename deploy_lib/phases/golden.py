"""Phase 4: plant the golden set, then clone every team box from it."""

from functools import partial

from common.constants import SNAP_BASE
from golden.golden_target_ops import _template_vmid_map
from golden.golden_ops import build_golden_set
from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
from nakonops.nakon_config_ops import generate_slot_golden_config
from multinode.placement_record import record_of
from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
from engine.template_freeze import load_template_hashes, save_template_hashes
from common.timing import timed
from common.utils import is_unmanaged, run_concurrent
from common.windows_ops import bootstrap_windows_box

from deploy_lib.coverage import record_coverage
from deploy_lib.golden_plan import golden_rebuild_gate
from deploy_lib.phases._snapshots import snap_base


def boot_win(ctx, t):
    print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
    gw = f"192.168.{t['identifier']}.1"
    with timed(ctx.comp_dir, 4, "bootstrap_windows", t["ip"]):
        bootstrap_windows_box(t.get("node", ctx.node), t["vmid"], t["ip"], gw,
                              "8.8.8.8", ctx.box_password)


def golden_coverage_recorder(ctx):
    # None = skip path (disk proven by hash): ensure the record, never erase a prior failure.
    # Tolerated plant failures are a real golden-disk gap and must reach verify's gate.
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
    # slot 0 is the engine node's map; drift rebuilds unless frozen, matching hashes short-circuit
    _golden_coverage = golden_coverage_recorder(ctx)

    # slot-0 goldens exist only when the engine node hosts teams — its vmbr<id> bridges live on the satellite hosts
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
        # built ON each satellite from its own templates — linked clones can't cross hosts
        # on separate storages. The plant rides the engine's gateway path; the jump routes + SNATs it there.
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
    ctx.state["golden_template_ids"] = golden_ids
    ctx.state["golden_ids_by_slot"] = {str(k): v for k, v in golden_ids_by_slot.items()}
    ctx.state["golden_hashes"] = ctx.golden_hashes
    save_template_hashes(ctx.comp_dir, golden={
        name: {"hash": ctx.golden_hashes[name], "inputs": ctx.golden_inputs[name]}
        for name in ctx.golden_hashes})
    ctx.save_state()
    # teardown must die goldens AFTER every clone, so the recorded node matters
    document = ctx.document()
    for name, vmid in golden_ids.items():
        document.record("golden", ctx.node, f"golden-{name}", vmid=vmid)
    if ctx.placement:
        for sat in ctx.placement["satellites"]:
            node = record_of(ctx.placement, sat["name"]).node
            for name, vmid in (golden_ids_by_slot.get(sat["slot"]) or {}).items():
                document.record("golden", node, f"golden-{name}", vmid=vmid)
    document.save()


def apply_team_boxes(ctx, golden_ids, golden_ids_by_slot):
    # keyed by vm_name: two box types can share a base template; any other key cross-wires golden disks
    from deploy_lib.phases._estate import create_team_boxes
    by_slot = {int(key): value for key, value in golden_ids_by_slot.items()}
    templates_by_node = {}
    sources = {}
    for target in ctx.all_targets:
        box, node = target["box"], target["node"]
        if is_unmanaged(box):
            templates_by_node.setdefault(node, _template_vmid_map(node))
            sources[target["vm_name"]] = int(templates_by_node[node][box["template"]])
        else:
            sources[target["vm_name"]] = int(by_slot[target["slot"] or 0][box["name"]])
    with timed(ctx.comp_dir, 4, "apply_team_boxes"):
        create_team_boxes(ctx, sources)


def bootstrap_clones(ctx):
    # a stale .postclone-swept marker lets a phase-5 resume skip the pre-domain pass on fresh clones
    (ctx.comp_dir / ".postclone-swept").unlink(missing_ok=True)

    win_results = run_concurrent(ctx.windows_targets, partial(boot_win, ctx), max_workers=4)
    for t, r in zip(ctx.windows_targets, win_results):
        if isinstance(r, Exception):
            raise r

    # managed only: unmanaged appliances take no SSH/cloud-init, and the in-path firewall
    # isn't routed until phase 5's cutover (its SNAP_BASE is taken there)
    with timed(ctx.comp_dir, 4, "wait_boxes_ssh"):
        wait_for_boxes_ssh(ctx.tf_ctx, ctx.managed_targets, timeout=300)
    with timed(ctx.comp_dir, 4, "wait_cloud_init"):
        wait_for_cloud_init(ctx.tf_ctx, ctx.managed_targets, timeout=240)
    with timed(ctx.comp_dir, 4, "fix_dns"):
        fix_dns_on_boxes(ctx.linux_targets, ctx.tf_ctx)
    with timed(ctx.comp_dir, 4, "prep_apt"):
        prep_apt_on_boxes(ctx.linux_targets, ctx.tf_ctx, use_proxy=ctx.apt_cache)

    print(f"  Snapshotting all managed boxes as '{SNAP_BASE}' (pre-sweep restore point)...")
    run_concurrent(ctx.managed_targets, partial(snap_base, ctx), max_workers=4)


def phase4_golden_set(ctx):
    if ctx.from_phase > 4:
        print("[4/8] Skipped (resume).")
        return

    print("[4/8] Building the golden set (plant once per box type, convert to template)...")
    stored = load_template_hashes(ctx.comp_dir)
    golden_ids, golden_ids_by_slot = build_goldens(ctx, stored)
    record_golden_set(ctx, golden_ids, golden_ids_by_slot)

    print("[4b/8] Cloning every team box as a linked clone of the golden set...")
    apply_team_boxes(ctx, golden_ids, golden_ids_by_slot)
    bootstrap_clones(ctx)
