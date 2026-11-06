"""The proxmox provider: the estate ops the deploy phases call.

The platform split (7c88059) moved the phase bodies out of deploy_lib/phases/ and
dispatches them through `providers.of_ctx(ctx)`; this package is the proxmox side of
that surface. Every op is the pre-split body (22decb92), verbatim: the bridges/engine
VM lifecycle, the golden plant/clone flow, the snapshot restore points, and the
teardown helpers.
"""

import json
from functools import partial
from pathlib import Path

from common.constants import (DEFAULT_ENGINE_MGMT_GW, ENGINE_TEMPLATE_NAME,
                              SNAP_BASE, SNAP_READY, red_bridge_name)
from common.timing import timed
from golden.golden_target_ops import _template_vmid_map
from infra.ownership import Ownership, bridge_comment
from multinode.placement_record import record_of, resolved_mgmt_bridge

PLATFORM = "proxmox"


# ---- shared context helpers (pre-split _estate.py) ----

def slot_team_identifiers(ctx):
    # Sorted order is load-bearing: it decides the engine's NIC order and the netplan ens numbering.
    slots = (ctx.placement or {}).get("team_slots") or {}
    return [ctx.teams[key]["identifier"] for key in sorted(ctx.teams)
            if slots.get(key, 0) == 0]


def has_in_path_firewall(ctx):
    return any(box.get("in_path") for box in ctx.boxes)


def planned_vmids(ctx):
    # The reclaim scan is filtered to these — phase 1 never hunts a node for things to delete.
    from engine.template_freeze import engine_template_vmid
    from multinode.placement_record import golden_vmid_for_slot
    satellites = (ctx.placement or {}).get("satellites", [])
    slots = [0] + [s["slot"] for s in satellites]
    return ({t["vmid"] for t in ctx.all_targets}
            | {ctx.engine_vmid, engine_template_vmid(ctx.engine_vmid)}
            | {golden_vmid_for_slot(ctx.engine_vmid, slot, i)
               for slot in slots for i in range(len(ctx.boxes))}
            | {s["jump_vmid"] for s in satellites})


def _team_bridges(ctx, document, node, identifiers, in_path):
    from estate import apply as estate_apply
    for identifier in identifiers:
        estate_apply.create_bridge(
            document, node, f"vmbr{identifier}",
            comment=bridge_comment(ctx.comp_name, ctx.run_id,
                                   f"Quotient team {identifier} — isolated, no uplink"),
            comp_name=ctx.comp_name, run_id=ctx.run_id)
        if in_path:
            estate_apply.create_bridge(
                document, node, f"vmbrW{identifier}",
                comment=bridge_comment(ctx.comp_name, ctx.run_id,
                                       f"Quotient team {identifier} — transit to in-path "
                                       f"firewall"),
                comp_name=ctx.comp_name, run_id=ctx.run_id)


def engine_build_ctx(ctx):
    from common.ssh_ops import quote_sshkeys
    from estate import config
    return {
        "ssh_key_path": ctx.ssh_key_abs,
        "vm_username": config.active().engine.username,
        "ssh_public_key_quoted": quote_sshkeys(config.active().ssh.public_key),
    }


# ---- phase 1: reclaim interrupted clones ----

def reclaim_interrupted(ctx):
    """Only the MARKER verdict (clone marker, no tags) is destroyed: OWNED is a live
    range (preflight refused it), MISSING/UNPROVEN is another run's or unknowable."""
    from infra.client import proxmox_api
    from infra.ownership import destroy_vm_if_exists, ownership_verdict
    wanted = planned_vmids(ctx)
    nodes = [ctx.node] + [record_of(ctx.placement, s["name"]).node
                          for s in (ctx.placement or {}).get("satellites", [])]
    for node in dict.fromkeys(nodes):
        try:
            vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        except Exception as e:                               # noqa: BLE001
            print(f"  WARNING: could not scan {node} for interrupted clones ({e}) — "
                  f"proceeding")
            continue
        for vm in vms:
            if vm["vmid"] not in wanted:
                continue
            verdict, _ = ownership_verdict(node, vm["vmid"], vm.get("tags"), ctx.reclaim_tags)
            if verdict != "marker":
                continue
            print(f"  vmid {vm['vmid']} carries this competition's clone marker but no tags "
                  f"(lock={vm.get('lock') or 'none'}) — reclaiming")
            destroy_vm_if_exists(node, vm["vmid"], expect_tags=ctx.reclaim_tags)


# ---- phase 2: engine template ----

def engine_template(ctx):
    from engine.engine_bootstrap_ops import bootstrap_scoring_engine
    from engine.engine_template_ops import (build_engine_template, destroy_engine_template,
                                            find_engine_template)
    from engine.template_freeze import (engine_hash_inputs, frozen_gate, hash_from_inputs,
                                        load_template_hashes, save_template_hashes,
                                        template_stamp_current)
    from common.utils import compfile_value
    from estate import config
    from infra.ownership import destroy_vm_if_exists, ownership_tags
    quotient_ref = compfile_value(ctx.comp_dir / "Compfile", "quotient_ref")
    engine_inputs = engine_hash_inputs(
        int(config.active().require("proxmox.template_vm_id")), quotient_ref,
        bootstrap_scoring_engine)
    engine_hash = hash_from_inputs(engine_inputs)
    stored = load_template_hashes(ctx.comp_dir)
    tmpl = find_engine_template(ctx.node, ctx.engine_vmid)
    rebuild = True
    if tmpl:
        entry = stored.get("engine") or {}
        # the node's own hash stamp decides reuse, not this worktree's file — a fresh
        # workspace still adopts the template a --teams-only teardown kept
        if template_stamp_current(ctx.node, tmpl, engine_hash):
            if entry.get("hash") != engine_hash:
                print("  Engine template reuses on the node's hash stamp (no local hash "
                      f"record in this worktree) — keeping (vmid {tmpl})")
            else:
                print(f"  Engine template hash matches — reusing (vmid {tmpl})")
            rebuild = False
        elif not frozen_gate(ctx.comp_dir, entry.get("inputs"), engine_inputs,
                             "engine template"):
            # frozen + code-only drift: frozen_gate warned; keep the frozen template.
            rebuild = False
    if rebuild:
        if tmpl:
            print("  Engine template hash differs — rebuilding...")
            # the build VM needs the planned mgmt IP and on a phase-2 resume the old engine
            # still holds it (phase 1 leaves a run's VMs alone); apply #1 recreates it as a clone
            destroy_vm_if_exists(ctx.node, ctx.engine_vmid, expect_tags=ctx.reclaim_tags)
            # The kept template carries the run that BUILT it, not the one replacing it, so a
            # rebuild reclaims this competition's template rather than refusing its own cache.
            left = destroy_engine_template(
                ctx.node, ctx.engine_vmid,
                expect_tags=ownership_tags(ctx.comp_name, None, "engine-template"))
            if left:
                from infra.ownership import Refusal
                raise Refusal(
                    saw="; ".join(left),
                    expected="the engine-template slot free, or a template this run owns",
                    remedy="let the run that owns it finish (or run its teardown), then "
                           "resume the deploy",
                    detail="cannot rebuild the engine template over another run's VM: "
                           + "; ".join(left))
            with timed(ctx.comp_dir, 2, "engine_template_build"):
                tmpl, build_info = build_engine_template(
                    ctx.node, ctx.comp_dir, ctx.engine_vmid,
                    int(config.active().require("proxmox.template_vm_id")),
                    engine_build_ctx(ctx), ctx.postgres_password, ctx.redis_password,
                    quotient_ref, engine_hash, engine_inputs, run_id=ctx.run_id,
                    mgmt_bridge=resolved_mgmt_bridge(ctx.placement))
            ctx.state["engine_build_info"] = build_info
        else:
            with timed(ctx.comp_dir, 2, "engine_template_build"):
                tmpl, build_info = build_engine_template(
                    ctx.node, ctx.comp_dir, ctx.engine_vmid,
                    int(config.active().require("proxmox.template_vm_id")),
                    engine_build_ctx(ctx), ctx.postgres_password, ctx.redis_password,
                    quotient_ref, engine_hash, engine_inputs, run_id=ctx.run_id,
                    mgmt_bridge=resolved_mgmt_bridge(ctx.placement))
            ctx.state["engine_build_info"] = build_info
    elif tmpl:
        # a kept template carries the earlier run's tag; re-stamp to THIS run so the next
        # teardown's strict guard and preflight recognize it
        from infra.ownership import retag_ownership
        retag_ownership(ctx.node, tmpl, ctx.comp_tags | {"engine-template"})
    from engine.template_freeze import save_template_hashes
    save_template_hashes(ctx.comp_dir, engine={"hash": engine_hash, "inputs": engine_inputs})
    ctx.state["engine_template_vmid"] = tmpl
    ctx.state["engine_template_hash"] = engine_hash
    ctx.save_state()
    # the teardown destroys what the document records, in the one order linked clones survive
    document = ctx.document()
    document.record("engine_template", ctx.node, ENGINE_TEMPLATE_NAME, vmid=tmpl)
    document.save()
    return tmpl


# ---- phase 3: engine + bridges ----

def create_estate(ctx):
    # Bridges before the engine; the cold boot surfaces the engine's team NICs.
    from estate import apply as estate_apply, engine_setup
    from common.utils import stages_red
    from estate import config
    cfg = config.active()
    document = ctx.document()
    ownership = Ownership(ctx.comp_name, ctx.run_id)
    teams = slot_team_identifiers(ctx)
    transit = teams if has_in_path_firewall(ctx) else []
    in_path = has_in_path_firewall(ctx)
    _team_bridges(ctx, document, ctx.node, teams, in_path)
    # A satellite's team boxes live on ITS host: vmbr<id> must exist there or the clone's
    # NIC cannot attach (qmstart: "bridge does not exist"). Preflight already requires the
    # name to be free on that host.
    from multinode.placement_record import record_of
    for sat in (ctx.placement or {}).get("satellites", []):
        sat_node = record_of(ctx.placement, sat["name"]).node
        sat_ids = [ctx.teams[key]["identifier"] for key in sat.get("teams") or []]
        _team_bridges(ctx, document, sat_node, sat_ids, in_path)
    red_bridge = red_bridge_name(ctx.run_id) if stages_red(ctx.comp_dir) else None
    if red_bridge:
        estate_apply.create_bridge(
            document, ctx.node, red_bridge,
            comment=bridge_comment(ctx.comp_name, ctx.run_id,
                                   "Quotient red segment — engine + red01 only, no uplink"),
            comp_name=ctx.comp_name, run_id=ctx.run_id)
    estate_apply.create_engine(
        document, ownership, ctx.node, vmid=ctx.engine_vmid,
        template_vmid=int(ctx.state["engine_template_vmid"]),
        team_identifiers=teams, transit_identifiers=transit,
        mgmt_ip=ctx.engine_mgmt_ip or "",
        mgmt_gw=(cfg.engine.mgmt_gw or DEFAULT_ENGINE_MGMT_GW),
        mgmt_bridge=resolved_mgmt_bridge(ctx.placement),
        username=cfg.engine.username,
        ssh_public_key=cfg.ssh.public_key,
        red_bridge=red_bridge)
    engine_setup.cold_boot(ctx.node, ctx.engine_vmid)
    # Read back after the cold boot: the NICs exist in the config from the PUT, but the
    # MAC list is what the netplan pins, and it must match the guests the boot surfaces.
    ctx.engine_nic_macs = estate_apply.engine_nic_macs(ctx.node, ctx.engine_vmid)


def create_team_boxes(ctx, sources):
    from estate import apply as estate_apply
    cfg = config.active()
    estate_apply.create_team_boxes(
        ctx.document(), Ownership(ctx.comp_name, ctx.run_id), ctx.all_targets,
        sources=sources, box_username=ctx.box_username, box_password=ctx.box_password,
        ssh_public_key=cfg.ssh.public_key)


def configure_engine_network(ctx, engine_ctx):
    # The satellite routes keep router traffic alive across an engine reboot mid-event.
    from estate import engine_setup
    from multinode.placement_record import satellite_routes_for
    from common.constants import RED_SEGMENT_GW, RED_SEGMENT_SUBNET
    from common.utils import stages_red
    from estate import config
    teams = slot_team_identifiers(ctx)
    engine_setup.configure_engine_network(
        engine_ctx, teams, teams if has_in_path_firewall(ctx) else [],
        satellite_routes_for(ctx.placement) if ctx.placement else [],
        red_segment=({"subnet": RED_SEGMENT_SUBNET, "gw": RED_SEGMENT_GW}
                     if stages_red(ctx.comp_dir) else None),
        macs=getattr(ctx, "engine_nic_macs", ()))


# ---- phase 4: golden set ----

def boot_win(ctx, t):
    from common.windows_ops import bootstrap_windows_box
    from golden.golden_ops import windows_plan_features
    print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
    gw = f"192.168.{t['identifier']}.1"
    box_name = (t.get("box") or {}).get("name") or t.get("name")
    with timed(ctx.comp_dir, 4, "bootstrap_windows", t["ip"]):
        bootstrap_windows_box(t.get("node", ctx.node), t["vmid"], t["ip"], gw,
                              "8.8.8.8", ctx.box_password,
                              features=windows_plan_features(ctx.comp_dir, box_name))


def golden_coverage_recorder(ctx):
    # None = skip path (disk proven by hash): ensure the record, never erase a prior failure.
    # Tolerated plant failures are a real golden-disk gap and must reach verify's gate.
    from guests.converge_ops import record_coverage
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


def build_golden_sets(ctx, stored, coverage_recorder):
    """Slot 0 is the engine node's map; drift rebuilds unless frozen, matching hashes
    short-circuit. Satellites build ON each satellite from their own templates — linked
    clones can't cross hosts on separate storages."""
    from common.ssh_ops import wait_for_boxes_ssh  # noqa: F401 (import parity with pre-split)
    from deploy_lib.golden_plan import golden_rebuild_gate
    from golden.golden_ops import build_golden_set
    from nakonops.nakon_config_ops import generate_slot_golden_config

    _golden_coverage = coverage_recorder or golden_coverage_recorder(ctx)
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
    from engine.template_freeze import save_template_hashes
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
    from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
    from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
    from guests.ubuntu_auth_ops import setup_ubuntu_auth
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
    # The clone's cloud-init rewrites resolv.conf/sudoers state; auth then DNS then apt is
    # the order the DNS fix's sudo calls need (docs/architecture.md phase 4).
    with timed(ctx.comp_dir, 4, "ubuntu_auth"):
        setup_ubuntu_auth(ctx.linux_targets, ctx.tf_ctx)
    with timed(ctx.comp_dir, 4, "fix_dns"):
        fix_dns_on_boxes(ctx.linux_targets, ctx.tf_ctx)
    with timed(ctx.comp_dir, 4, "prep_apt"):
        prep_apt_on_boxes(ctx.linux_targets, ctx.tf_ctx, use_proxy=ctx.apt_cache)

    print(f"  Snapshotting all managed boxes as '{SNAP_BASE}' (pre-sweep restore point)...")
    run_concurrent(ctx.managed_targets, partial(snap_base, ctx), max_workers=4)


# ---- restore points (pre-split _snapshots.py) ----

def restore_point_create(ctx, target, name, description=""):
    from infra.lifecycle import take_snapshot
    with timed(ctx.comp_dir, 4, "snapshot", target.get("vm_name") or target.get("name")):
        take_snapshot(target.get("node", ctx.node), target["vmid"], name,
                      description=description)


def restore_points_list(ctx, target):
    from infra.lifecycle import list_snapshots
    return list_snapshots(target.get("node", ctx.node), target["vmid"])


def restore_point_rollback(ctx, target, name):
    from infra.lifecycle import rollback_snapshot
    rollback_snapshot(target.get("node", ctx.node), target["vmid"], name)


def restore_point_hint(ctx, target):
    from infra.lifecycle import snapshot_support_hint
    return snapshot_support_hint(target.get("node", ctx.node), target["vmid"])


def snap_base(ctx, t, phase=4):
    restore_point_create(ctx, t, SNAP_BASE,
                         description="tezcatlipoca: booted, networked, pre-sweep")


def snap_ready(ctx, t):
    restore_point_create(ctx, t, SNAP_READY,
                         description="tezcatlipoca: as delivered, post-sweep + hardening")


# ---- teardown helpers (pre-split teardown/machinery.py + estate/destroy.py) ----

def live_unit_numbers():
    from infra.client import live_vmids
    return live_vmids()


def destroy_estate(document, *, full=True, nodes=None, log=print):
    from estate.destroy import destroy_estate as _destroy
    return _destroy(document, full=full, nodes=nodes, log=log)


def sweep_tagged_leftovers(nodes, ownership, keep_templates=False):
    from teardown.machinery import sweep_tagged_leftovers as _sweep
    return _sweep(nodes, ownership, keep_templates=keep_templates)


def pre_stop_boxes(teams, boxes, default_node, team_nodes=None, ownership=None):
    from teardown.machinery import pre_stop_windows_boxes
    pre_stop_windows_boxes(teams, boxes, default_node,
                           team_nodes=team_nodes, ownership=ownership)


def red01_deploy(comp_dir, **kwargs):
    from red import red01_ops
    return red01_ops.deploy(comp_dir, **kwargs)


def red01_destroy(comp_dir, **kwargs):
    from red import red01_ops
    return red01_ops.destroy(comp_dir, **kwargs)


def red01_occupant_verdict(vmid, comp, run_id=None):
    from infra import red01
    return red01.occupant_verdict(vmid, comp, run_id)


def windows_bootstrap(ctx, target, features=()):
    """The proxmox Windows bootstrap rides the golden phase's own call site (the phase
    imports common.windows_ops directly); the provider op exists so a caller that only
    has the provider surface still gets the real behavior."""
    from common.windows_ops import bootstrap_windows_box
    gw = f"192.168.{target['identifier']}.1"
    box_name = (target.get("box") or {}).get("name") or target.get("name")
    bootstrap_windows_box(target.get("node", ctx.node), target["vmid"], target["ip"], gw,
                          "8.8.8.8", ctx.box_password, features=features)
