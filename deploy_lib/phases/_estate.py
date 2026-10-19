"""Estate plumbing shared by the deploy phases; stubbed by tests/_deploy_patch.dpatch."""


from common.constants import DEFAULT_ENGINE_MGMT_GW
from engine.engine_template_ops import _discover_vm_ipv4
from infra.guest_exec import wait_for_guest_agent
from multinode.placement_record import resolved_mgmt_bridge
from estate import config

ENGINE_NAME = "quotient-engine"


def discover_engine_ip(node, vmid, timeout=300):
    wait_for_guest_agent(node, vmid, timeout=timeout)
    ip = _discover_vm_ipv4(node, vmid)
    if not ip:
        raise RuntimeError(
            f"could not discover the engine's management IP on vmid {vmid} — the guest "
            f"agent answered no usable address. Set TEZ_ENGINE_MGMT_IP (portable-node "
            f"mode) or fix the agent channel.")
    return ip


def engine_context(ctx):
    """A DHCP engine has no planned address, so the running VM's is discovered via the guest agent."""
    from estate import engine_setup
    engine_ctx = engine_setup.engine_ctx(ctx.comp_dir)
    if ctx.engine_mgmt_ip:
        engine_ctx["scoring_engine_ip"] = ctx.engine_mgmt_ip
    if not engine_ctx["scoring_engine_ip"]:
        engine_ctx["scoring_engine_ip"] = discover_engine_ip(ctx.node, ctx.engine_vmid)
    return {**engine_ctx, "box_username": ctx.box_username, "event_name": ctx.name}


def slot_team_identifiers(ctx):
    # Sorted order is load-bearing: it decides the engine's NIC order and the netplan ens numbering.
    slots = (ctx.placement or {}).get("team_slots") or {}
    return [ctx.teams[key]["identifier"] for key in sorted(ctx.teams)
            if slots.get(key, 0) == 0]


def has_in_path_firewall(ctx):
    return any(box.get("in_path") for box in ctx.boxes)


def create_engine_and_bridges(ctx):
    # Bridges before the engine; the cold boot surfaces the engine's team NICs.
    from estate import apply as estate_apply, engine_setup
    from infra.ownership import Ownership, bridge_comment
    from common.constants import red_bridge_name
    from common.utils import stages_red
    cfg = config.active()
    document = ctx.document()
    ownership = Ownership(ctx.comp_name, ctx.run_id)
    teams = slot_team_identifiers(ctx)
    transit = teams if has_in_path_firewall(ctx) else []
    for identifier in teams:
        estate_apply.create_bridge(
            document, ctx.node, f"vmbr{identifier}",
            comment=bridge_comment(ctx.comp_name, ctx.run_id,
                                   f"Quotient team {identifier} — isolated, no uplink"))
    for identifier in transit:
        estate_apply.create_bridge(
            document, ctx.node, f"vmbrW{identifier}",
            comment=bridge_comment(ctx.comp_name, ctx.run_id,
                                   f"Quotient team {identifier} — transit to in-path firewall"))
    red_bridge = red_bridge_name(ctx.run_id) if stages_red(ctx.comp_dir) else None
    if red_bridge:
        estate_apply.create_bridge(
            document, ctx.node, red_bridge,
            comment=bridge_comment(ctx.comp_name, ctx.run_id,
                                   "Quotient red segment — engine + red01 only, no uplink"))
    estate_apply.create_engine(
        document, ownership, ctx.node, vmid=ctx.engine_vmid,
        template_vmid=int(ctx.state["engine_template_vmid"]),
        team_identifiers=teams, transit_identifiers=transit,
        mgmt_ip=ctx.engine_mgmt_ip or "",
        mgmt_gw=(config.active().engine.mgmt_gw or DEFAULT_ENGINE_MGMT_GW),
        mgmt_bridge=resolved_mgmt_bridge(ctx.placement),
        username=cfg.engine.username,
        ssh_public_key=cfg.ssh.public_key,
        red_bridge=red_bridge)
    engine_setup.cold_boot(ctx.node, ctx.engine_vmid)


def create_team_boxes(ctx, sources):
    from estate import apply as estate_apply
    from infra.ownership import Ownership
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
    teams = slot_team_identifiers(ctx)
    engine_setup.configure_engine_network(
        engine_ctx, teams, teams if has_in_path_firewall(ctx) else [],
        satellite_routes_for(ctx.placement) if ctx.placement else [],
        red_segment=({"subnet": RED_SEGMENT_SUBNET, "gw": RED_SEGMENT_GW}
                     if stages_red(ctx.comp_dir) else None))
