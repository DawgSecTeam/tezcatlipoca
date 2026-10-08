"""The single pre-Proxmox-mutation entry point: `run_preflight(plan)`.

Every check that must pass before a deploy touches Proxmox runs here, in this order:

  1. concurrency      - no other deploy live on this host (flock signal)         [once]
  2. per node share   - templates resolve + carry cloud-init + engine base image + jump source;
                        vmid/bridge collisions (ownership-proved); datastore headroom
  3. mgmt IPs         - engine static IP, satellite jump IPs vs running guests (cross-node)
  4. catalog          - `nakon catalog check` over the competition's pins
  5. pins (advisory)  - warns when a managed box carries zero pins (never refuses)
  6. portal (advisory) - Compfile `portal 1`: console-token privileges, tunnel-token
                        hygiene in practice worktrees (never refuses)

`preflight_gates` (single-node) and `preflight_gates_multinode` build a plan and call it; they are
kept as thin wrappers so deploy.py's call sites and config_ops' re-exports are unchanged.
Teardown/redeploy mutate under the same ownership proof (vm_ownership.ownership_verdict) - see
destroy_vm_if_exists - so "ours" has one definition everywhere."""

from constants import SCORING_ENGINE_VMID
from preflight import catalog, concurrency, headroom, mgmt_ip, pins, portal
from preflight.clashes import check_collisions
from preflight.plan import multinode_plan, single_node_plan
from preflight.templates import check_share_templates


def run_preflight(plan):
    concurrency.gate_concurrent_deploys()
    for share in plan.shares:
        share.raw_vms = share.fetch_vms()
        vms = ([vm for vm in share.raw_vms if vm.get("node") == share.node]
               if share.scope_to_node else share.raw_vms)
        if share.check_templates or share.engine_base:
            check_share_templates(share, plan.boxes, vms)
        if share.check_collisions:
            check_collisions(share, plan, vms)
        if share.check_headroom:
            headroom.check_datastore_headroom(
                share.node, share.datastore, plan.boxes, share.num_teams,
                label=share.label, node_name=share.name or None)
    if plan.engine_mgmt_ip:
        engine = next(s for s in plan.shares if s.is_engine)
        mgmt_ip.engine_mgmt_ip_gate(engine.node, engine.raw_vms, plan.engine_vmid,
                                    plan.engine_mgmt_ip, ours_tags=plan.our_tags)
    if plan.placement:
        mgmt_ip.jump_mgmt_ip_gate(plan)
    catalog.catalog_gate(plan.comp_dir)
    pins.warn_unpinned_boxes(plan.comp_dir, plan.boxes)
    portal.portal_gate(plan.comp_dir, plan.placement)


def preflight_gates(comp_dir, boxes, num_teams, teams=None, engine_vmid=SCORING_ENGINE_VMID,
                    check_free=True, engine_mgmt_ip=None, our_run_tag=None):
    """Single-node preflight (a one-share plan over the whole cluster view).

    The collision gate (check_free) makes concurrent competitions on one node safe: it fails
    fast if this comp's engine vmid, any team vmid, or any team bridge already exists (i.e.
    belongs to another range). our_run_tag (the prior state's run id) scopes the "ours"
    exemption to THIS deploy's lineage — a same-comp VM without it is another run's and stays
    a collision (2026-10-02 near-miss)."""
    run_preflight(single_node_plan(comp_dir, boxes, num_teams, teams, engine_vmid, check_free,
                                   engine_mgmt_ip, our_run_tag))


def preflight_gates_multinode(comp_dir, boxes, teams, engine_vmid, placement,
                              engine_mgmt_ip=None, check_free=True, our_run_tag=None):
    """Per-node preflight for a multi-node placement: every hosting node gets its own
    template/collision/headroom gate scoped to exactly what it will hold (engine node:
    engine base + slot-0 goldens + its teams; each satellite: its slot's goldens, the
    jump VM, its teams). A satellite that would have died mid-clone on another host fails
    here instead."""
    run_preflight(multinode_plan(comp_dir, boxes, teams, engine_vmid, placement,
                                 engine_mgmt_ip, check_free, our_run_tag))
