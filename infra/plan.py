"""What to check, and where: a PreflightPlan is a list of NodeShares (one for single-node, one per hosting node for multi-node)."""

from dataclasses import dataclass, field
from typing import Callable, Optional

from infra.client import proxmox_api
from estate import config
from common.constants import comp_tag


@dataclass
class NodeShare:
    name: str
    node: str
    multi: bool                    # multi-node wording/ordering (one flag, message level only)
    slot: int
    is_engine: bool
    teams: dict
    num_teams: int
    datastore: str
    fetch_vms: Callable
    scope_to_node: bool = False
    engine_base: Optional[Callable] = None
    check_templates: bool = True
    needs_jump_template: bool = False
    jump_template: str = ""
    check_collisions: bool = True
    check_headroom: bool = True
    raw_vms: list = field(default_factory=list)

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
    red01_vmid: int = 0                # the comp's reserved red01 (one stride past the goldens)
    red01_ip: str = ""                 # '' until deploy-time selection
    red01_ip_explicit: bool = False    # chosen (operator or proven): unverifiable warns, never refuses
    resuming: bool = False             # collision gates relax for THIS comp's own leftovers

    @property
    def comp_name(self):
        from common.utils import competition_name
        return competition_name(self.comp_dir)

    @property
    def our_tags(self):
        """This deploy's FULL ownership set; without a run tag a same-comp VM is another run's and must classify as a collision, not ours."""
        tags = {"tezcatlipoca", comp_tag(self.comp_name)}
        if self.our_run_tag:
            tags.add(self.our_run_tag)
        return tags


def _fetch_cluster_vms_single():
    try:
        return proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    except Exception as e:
        raise SystemExit(
            f"  ERROR: Proxmox API unreachable during preflight: {e}\n"
            f"         This is the liveness check that counts — do NOT judge the node by "
            f"ping: during that outage the bridge answered ICMP while forwarding "
            f"nothing. Probe the API port directly (curl -k https://<node>:8006/ — any "
            f"HTTP code except 000 is up). Deploy state survives an outage; on recovery "
            f"resume with --from-phase rather than restarting.")


def single_node_plan(comp_dir, boxes, num_teams, teams, engine_vmid, check_free,
                     engine_mgmt_ip, our_run_tag):
    from red import red01_ops
    share = NodeShare(
        name="", node=config.active().proxmox.node, multi=False, slot=0, is_engine=True,
        teams=teams or {}, num_teams=num_teams,
        datastore=config.active().proxmox.datastore,
        fetch_vms=_fetch_cluster_vms_single,
        engine_base=lambda: int(config.require("proxmox.template_vm_id")),
        check_collisions=bool(check_free and teams))
    return PreflightPlan(comp_dir, boxes, engine_vmid, [share], our_run_tag=our_run_tag,
                         engine_mgmt_ip=engine_mgmt_ip,
                         red01_vmid=red01_ops.red01_vmid_for(comp_dir, engine_vmid),
                         red01_ip=red01_ops.red01_ip_for(comp_dir),
                         red01_ip_explicit=red01_ops.red01_ip_is_explicit(comp_dir),
                         resuming=not check_free)


def multinode_plan(comp_dir, boxes, teams, engine_vmid, placement, engine_mgmt_ip,
                   check_free, our_run_tag):
    from red import red01_ops
    from multinode import placement_record
    from infra.client import cluster_vms_for

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
            fetch_vms=(lambda n=rec.node: cluster_vms_for(n)), scope_to_node=True,
            engine_base=(lambda r=rec: r.engine_base_vmid) if is_engine else None,
            check_templates=bool(node_teams),
            needs_jump_template=bool(node_teams) and slot > 0,
            jump_template=getattr(rec, "jump_template", ""),
            # resume: this comp's own leftovers are the create path's to reconcile
            check_collisions=bool(check_free and not unused_satellite),
            check_headroom=bool(check_free and not unused_satellite)))
    red = placement.get("red01") or {}
    return PreflightPlan(comp_dir, boxes, engine_vmid, shares, our_run_tag=our_run_tag,
                         engine_mgmt_ip=engine_mgmt_ip, placement=placement,
                         red01_vmid=red.get("vmid")
                         or red01_ops.red01_vmid_for(comp_dir, engine_vmid, placement),
                         red01_ip=red.get("ip") or "",
                         # A recorded address was typed or proven free when selected; not re-litigated.
                         red01_ip_explicit=bool(red.get("ip")),
                         resuming=not check_free)
