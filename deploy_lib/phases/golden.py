"""Phase 4: plant the golden set, then clone every team box from it."""

from functools import partial

import providers

from common.constants import SNAP_BASE
from golden.golden_target_ops import _template_vmid_map
from golden.golden_ops import windows_plan_features
from guests.apt_dns_ops import fix_dns_on_boxes, prep_apt_on_boxes
from guests.ubuntu_auth_ops import setup_ubuntu_auth
from nakonops.nakon_config_ops import generate_slot_golden_config  # noqa: F401  (slot goldens print their anchor in the proxmox provider)
from multinode.placement_record import record_of
from common.ssh_ops import wait_for_boxes_ssh, wait_for_cloud_init
from engine.template_freeze import load_template_hashes, save_template_hashes
from common.timing import timed
from common.utils import is_unmanaged, run_concurrent
from common.windows_ops import bootstrap_windows_box  # bound here: tests patch the phase's proxmox call

from guests.converge_ops import record_coverage
from deploy_lib.golden_plan import golden_rebuild_gate  # noqa: F401  (patch seam for the resume tests)
from deploy_lib.phases._snapshots import snap_base


def boot_win(ctx, t):
    print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
    box_name = (t.get("box") or {}).get("name") or t.get("name")
    features = windows_plan_features(ctx.comp_dir, box_name)
    with timed(ctx.comp_dir, 4, "bootstrap_windows", t["ip"]):
        if providers.platform_of_ctx(ctx) == providers.PROXMOX:
            gw = f"192.168.{t['identifier']}.1"
            bootstrap_windows_box(t.get("node", ctx.node), t["vmid"], t["ip"], gw,
                                  "8.8.8.8", ctx.box_password,
                                  features=features)
        else:
            providers.of_ctx(ctx).windows_bootstrap(ctx, t, features=features)


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
    # Slot-0 build had no timed() wrapper, so a phase-4 golden build was invisible in the
    # timings and read as dead time (an 83-minute hole in one deploy went unattributed).
    with timed(ctx.comp_dir, 4, "golden_build"):
        return providers.of_ctx(ctx).build_golden_sets(ctx, stored,
                                                       golden_coverage_recorder(ctx))


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
