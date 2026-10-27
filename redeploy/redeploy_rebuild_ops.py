"""Redeploy mode rebuild: re-clone a box from its golden template and re-run the convergence.

The clone itself goes through `estate.apply.create_team_box` — the same creator the deploy
uses. A hand-rolled clone here had already drifted from it (no guest agent, no DNS
nameserver, no post-clone 400 retry, no disk auto-detection, no `in_path` NIC order), and the
box it produced was not the box the deploy would have produced.
"""

import pipeline_api

from common.targets import describe_target
from common.utils import box_dns_primary, load_users_config
from engine.template_freeze import stored_template_hash
from estate import apply as estate_apply
from estate import config as estate_config
from estate import state as estate_state
from golden.golden_ops import windows_plan_features
from infra.ownership import Ownership, destroy_vm_if_exists
from redeploy.redeploy_plant_ops import post_base_converge
from redeploy.redeploy_select_ops import box_platform


def _golden_for(rctx, t):
    """The golden vmid this box is rebuilt from, in this box's node slot."""
    state, comp_dir = rctx.state, rctx.comp_dir
    by_slot = state.get("golden_ids_by_slot") or {}
    if by_slot:
        from multinode.placement_record import read_placement, slot_of_team
        placement = read_placement(comp_dir)
        slot = slot_of_team(placement, t["team_key"]) if placement else 0
        slot_ids = by_slot.get(str(slot)) or {}
    else:
        slot_ids = state.get("golden_template_ids") or {}
    name = t["box"]["name"]
    if name not in slot_ids:
        raise SystemExit(
            f"  ERROR: state has no golden template for '{t['box']['template']}' "
            f"(slot golden ids: {sorted(slot_ids)}) — a rebuild from the ORIGINAL "
            f"template would miss every golden-stage install (services, vulns). "
            f"Redeploy the range instead.")
    vmid = int(slot_ids[name])
    expected = (state.get("golden_hashes") or {}).get(name)
    on_node = stored_template_hash(t.get("node") or rctx.node, vmid)
    if expected and on_node and on_node != expected:
        print(f"    WARNING: golden template for '{name}' on the node "
              f"({on_node[:12]}) differs from the verified hash "
              f"({expected[:12]}) — rebuilding from the node's template "
              f"anyway. If this competition is frozen, investigate after the event.")
    return vmid


def mode_rebuild(targets, rctx, nakon_config_path, nakon_bundle):
    node, comp_dir, state = rctx.node, rctx.comp_dir, rctx.state
    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: the state document has no box_password — can't recreate a box with the "
            "login the rest of the range uses. Rebuild is unavailable for this competition."
        )
    ssh_public_key = estate_config.active().ssh.public_key
    box_username, _credlist = load_users_config(comp_dir)
    ownership = Ownership(rctx.comp_name, state["run_id"])
    document = estate_state.EstateState.load(comp_dir)

    rebuilt = []
    for t in targets:
        tnode = t.get("node") or node
        source = _golden_for(rctx, t)
        print(f"  Rebuilding {describe_target(t)} from golden vmid {source} (linked clone)...")
        # The state's run id is the destruction anchor: a box belonging to another run is refused.
        destroy_vm_if_exists(tnode, t["vmid"], expect_tags=ownership.tags)
        estate_apply.create_team_box(
            document, ownership, {**t, "node": tnode}, source_vmid=source,
            box_username=box_username, box_password=box_password,
            ssh_public_key=ssh_public_key)
        if box_platform(t["box"]) == "windows":
            print(f"    Bootstrapping Windows box {t['ip']} (vmid {t['vmid']})...")
            pipeline_api.bootstrap_windows_box(
                tnode, t["vmid"], t["ip"], f"192.168.{t['identifier']}.1",
                box_dns_primary(), box_password,
                features=windows_plan_features(comp_dir, t["box"]["name"]))
        rebuilt.append(t)
        print(f"    NOTE: {t['vm_name']} was recreated outside the deploy. It carries this "
              f"run's ownership tags, so the next deploy adopts it instead of rebuilding.")

    pipeline_api.wait_for_boxes_ssh(rctx.ctx, rebuilt, timeout=600)
    pipeline_api.wait_for_cloud_init(rctx.ctx, rebuilt, timeout=300)
    post_base_converge(rebuilt, rctx, take_base=True)
    return rebuilt
