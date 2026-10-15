"""The estate plumbing the deploy phases share: the engine's coordinates, the create call,
and the guest-side network setup.

This replaces `_terraform.py` — the phases call these, and the offline suite stubs them the
same way it stubbed the terraform runner (tests/_deploy_patch.dpatch patches every
deploy_lib module binding a name).
"""


from common.constants import DEFAULT_ENGINE_MGMT_GW
from engine.engine_template_ops import _discover_vm_ipv4
from infra.guest_exec import wait_for_guest_agent
from estate import config

ENGINE_NAME = "quotient-engine"


def discover_engine_ip(node, vmid, timeout=300):
    """The running engine's management IPv4, via the guest agent."""
    wait_for_guest_agent(node, vmid, timeout=timeout)
    ip = _discover_vm_ipv4(node, vmid)
    if not ip:
        raise RuntimeError(
            f"could not discover the engine's management IP on vmid {vmid} — the guest "
            f"agent answered no usable address. Set TF_VAR_engine_mgmt_ip (portable-node "
            f"mode) or fix the agent channel.")
    return ip


def engine_context(ctx):
    """The dict the SSH helpers expect (ssh_key_path / vm_username / scoring_engine_ip).

    On a DHCP engine the planned address is empty, so the running VM's is discovered
    through the guest agent; a static one is taken from the state document.
    """
    from estate import engine_setup
    engine_ctx = engine_setup.engine_ctx(ctx.comp_dir)
    if ctx.engine_mgmt_ip:
        engine_ctx["scoring_engine_ip"] = ctx.engine_mgmt_ip
    if not engine_ctx["scoring_engine_ip"]:
        engine_ctx["scoring_engine_ip"] = discover_engine_ip(ctx.node, ctx.engine_vmid)
    return {**engine_ctx, "box_username": ctx.box_username, "event_name": ctx.name}


def slot_team_identifiers(ctx):
# Slot 0's team identifiers, in the order the old terraform sorted them — the order is
#     load-bearing: it decides both the engine's NIC order and the netplan's ens numbering.
    slots = (ctx.placement or {}).get("team_slots") or {}
    return [ctx.teams[key]["identifier"] for key in sorted(ctx.teams)
            if slots.get(key, 0) == 0]


def has_in_path_firewall(ctx):
    return any(box.get("in_path") for box in ctx.boxes)


def create_engine_and_bridges(ctx):
# The team bridges, then the engine, then the cold boot that surfaces its team NICs.
    from estate import apply as estate_apply, engine_setup
    from infra.ownership import Ownership
    cfg = config.active()
    document = ctx.document()
    ownership = Ownership(ctx.comp_name, ctx.run_id)
    teams = slot_team_identifiers(ctx)
    transit = teams if has_in_path_firewall(ctx) else []
    for identifier in teams:
        estate_apply.create_bridge(document, ctx.node, f"vmbr{identifier}",
                                   comment=f"Quotient team {identifier} — isolated, no uplink")
    for identifier in transit:
        estate_apply.create_bridge(
            document, ctx.node, f"vmbrW{identifier}",
            comment=f"Quotient team {identifier} — transit to in-path firewall")
    estate_apply.create_engine(
        document, ownership, ctx.node, vmid=ctx.engine_vmid,
        template_vmid=int(ctx.state["engine_template_vmid"]),
        team_identifiers=teams, transit_identifiers=transit,
        mgmt_ip=ctx.engine_mgmt_ip or "",
        mgmt_gw=(config.active().engine.mgmt_gw or DEFAULT_ENGINE_MGMT_GW),
        datastore=cfg.proxmox.datastore, username=cfg.engine.username,
        ssh_public_key=cfg.ssh.public_key)
    engine_setup.cold_boot(ctx.node, ctx.engine_vmid)


def create_team_boxes(ctx, sources):
# Clone every team box from its golden (slot-aware source vmids, keyed by vm_name).
    from estate import apply as estate_apply
    from infra.ownership import Ownership
    cfg = config.active()
    estate_apply.create_team_boxes(
        ctx.document(), Ownership(ctx.comp_name, ctx.run_id), ctx.all_targets,
        sources=sources, box_username=ctx.box_username, box_password=ctx.box_password,
        ssh_public_key=cfg.ssh.public_key, datastore=cfg.proxmox.datastore)


def configure_engine_network(ctx, engine_ctx):
# Address the engine's team bridges; the satellite routes keep router traffic alive across
#     an engine reboot mid-event.
    from estate import engine_setup
    from multinode.placement_record import satellite_routes_for
    teams = slot_team_identifiers(ctx)
    engine_setup.configure_engine_network(
        engine_ctx, teams, teams if has_in_path_firewall(ctx) else [],
        satellite_routes_for(ctx.placement) if ctx.placement else [])
