"""What to check, and where: a PreflightPlan is a list of NodeShares.

Single-node is simply a plan with ONE share (the whole cluster view, slot 0); multi-node is one
share per hosting node, each scoped to exactly what that node will hold. All checking code is
parametrized by the share - there is no separate single-node implementation."""

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from pve_api import proxmox_api


@dataclass
class NodeShare:
    name: str                      # nodes.json name ("" for single-node)
    node: str                      # PVE hostname (API paths)
    multi: bool                    # multi-node wording/ordering (one flag, message level only)
    slot: int
    is_engine: bool
    teams: dict                    # team_key -> {"identifier": ...} hosted on this share
    num_teams: int
    datastore: str
    fetch_vms: Callable            # () -> cluster resource list as this share sees it
    scope_to_node: bool = False    # filter the VM view to vm["node"] == node
    engine_base: Optional[Callable] = None   # () -> engine base vmid that must exist here
    check_templates: bool = True
    needs_jump_template: bool = False
    jump_template: str = ""
    check_collisions: bool = True
    check_headroom: bool = True
    raw_vms: list = field(default_factory=list)   # filled by the runner

    @property
    def label(self):
        return f"[{self.name}]" if self.multi else ""

    @property
    def where(self):
        return f"node '{self.name}' ({self.node})" if self.multi else f"node '{self.node}'"


@dataclass
class PreflightPlan:
    comp_dir: object
    boxes: list
    engine_vmid: int
    shares: list
    our_run_tag: Optional[str] = None
    engine_mgmt_ip: Optional[str] = None
    placement: Optional[dict] = None   # multi-node only (jump-IP sweep)

    @property
    def comp_name(self):
        return self.comp_dir.name

    @property
    def our_tags(self):
        """This deploy's FULL ownership set (comp tag + run id). Without a run tag a same-comp
        VM is another run's and must classify as a collision, not as ours."""
        tags = {"tezcatlipoca", f"comp-{self.comp_name}"}
        if self.our_run_tag:
            tags.add(self.our_run_tag)
        return tags


def _fetch_cluster_vms_single():
    try:
        return proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    except Exception as e:
        # The API port IS the node's liveness signal. A site outage once left the tailnet
        # bridge answering ICMP *for itself* while forwarding nothing, so "ping works"
        # recovered hours before any TCP did (2026-09-24, ~10h outage) — never diagnose a
        # node from ping. `curl -k https://<node>:8006/` returning any HTTP code but 000
        # is the check that means something.
        raise SystemExit(
            f"  ERROR: Proxmox API unreachable during preflight: {e}\n"
            f"         This is the liveness check that counts — do NOT judge the node by "
            f"ping: during the 2026-09-24 outage the bridge answered ICMP while forwarding "
            f"nothing. Probe the API port directly (curl -k https://<node>:8006/ — any "
            f"HTTP code except 000 is up). Deploy state survives an outage; on recovery "
            f"resume with --from-phase rather than restarting.")


def single_node_plan(comp_dir, boxes, num_teams, teams, engine_vmid, check_free,
                     engine_mgmt_ip, our_run_tag):
    share = NodeShare(
        name="", node=os.environ["TF_VAR_proxmox_node"], multi=False, slot=0, is_engine=True,
        teams=teams or {}, num_teams=num_teams,
        datastore=os.environ.get("TF_VAR_datastore", "local-lvm"),
        fetch_vms=_fetch_cluster_vms_single,
        engine_base=lambda: int(os.environ["TF_VAR_template_vm_id"]),
        check_collisions=bool(check_free and teams))
    return PreflightPlan(comp_dir, boxes, engine_vmid, [share], our_run_tag=our_run_tag,
                         engine_mgmt_ip=engine_mgmt_ip)


def multinode_plan(comp_dir, boxes, teams, engine_vmid, placement, engine_mgmt_ip,
                   check_free, our_run_tag):
    # Lazy: only the multi-node path pulls in the placement machinery.
    import placement_record
    import pve_api

    # In-path firewalls are a slot-0 feature for now: the transit bridge + gateway
    # handoff live on the engine node, while each satellite's jump VM impersonates
    # 192.168.<id>.1 for its local teams — an in-path firewall there would fight the
    # jump for the gateway address (docs/multi-node.md).
    if any(b.get("in_path") for b in boxes) and any(
            s != 0 for s in placement.get("team_slots", {}).values()):
        raise SystemExit(
            "  ERROR: in_path firewalls are only supported on engine-node (slot 0) teams — "
            "the satellite design gives 192.168.<id>.1 to the jump VM, so an in-path "
            "firewall there would fight it for the gateway address. Move the teams to the "
            "engine node or drop the firewall from the lineup.")
    engine_name = placement["engine_node"]
    shares = []
    for node_name in placement["slots"]:
        rec = placement_record.record_of(placement, node_name)
        slot = placement["slots"][node_name]
        node_teams = {k: teams[k] for k in placement_record.teams_on_node(placement, node_name)}
        is_engine = node_name == engine_name
        unused_satellite = not node_teams and slot > 0
        shares.append(NodeShare(
            name=node_name, node=rec.node, multi=True, slot=slot, is_engine=is_engine,
            teams=node_teams, num_teams=len(node_teams), datastore=rec.datastore,
            fetch_vms=(lambda n=rec.node: pve_api.cluster_vms_for(n)), scope_to_node=True,
            engine_base=(lambda r=rec: r.engine_base_vmid) if is_engine else None,
            check_templates=bool(node_teams),
            needs_jump_template=bool(node_teams) and slot > 0,
            jump_template=getattr(rec, "jump_template", ""),
            # resume (check_free off): this competition's own leftovers are
            # phase-1/terraform's to reconcile
            check_collisions=bool(check_free and not unused_satellite),
            check_headroom=bool(check_free and not unused_satellite)))
    return PreflightPlan(comp_dir, boxes, engine_vmid, shares, our_run_tag=our_run_tag,
                         engine_mgmt_ip=engine_mgmt_ip, placement=placement)
