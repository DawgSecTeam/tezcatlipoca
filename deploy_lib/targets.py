"""Enumerate every VM the deploy touches and split them into the phases' work lists."""


from common.targets import enumerate_targets, persist_targets
from common.utils import is_unmanaged
from common.windows_ops import is_windows_template
from estate import config


def enumerate_deploy_targets(ctx, comp_dir):
# Enumerate every VM this deploy will touch, onto `ctx`.
#
#     node is read from the env here (the engine's host, already selected by placement);
#     all_targets keeps every positional vmid, while the Linux/Windows work lists drop
#     unmanaged boxes — pfSense/appliances have no plant/pre-domain/fix_services/cloud-init,
#     they clone from their own template and self-configure (but must stay in all_targets
#     for vmid arithmetic).
    ctx.node = config.active().proxmox.node
    ctx.all_targets = enumerate_targets(ctx.teams, ctx.boxes,
                                           placement=ctx.placement, default_node=ctx.node)
    persist_targets(comp_dir, ctx.all_targets, ctx.boxes)
    # Mirror the frozen roster into the state document: targets.json is the reorder-safe
    # record the deploy tools read, and the document is what a consumer of the run's state
    # looks at (migration filled this section for legacy ranges; nothing filled it here).
    ctx.comp_dir = ctx.comp_dir or comp_dir
    document = ctx.document()
    document.section("roster")["targets"] = {
        t["vm_name"]: {"vmid": t["vmid"], "ip": t["ip"], "node": t["node"],
                       "slot": t["slot"], "box_name": t["box_name"], "team_key": t["team_key"]}
        for t in ctx.all_targets}
    document.save()
    # Unmanaged boxes (pfSense/appliances) get no plant/pre-domain/fix_services/cloud-init —
    # they are cloned from their own template and self-configure. Keep them in all_targets
    # (positional vmids) but out of the Linux/Windows work lists.
    ctx.managed_targets = [t for t in ctx.all_targets if not is_unmanaged(t["box"])]
    ctx.linux_targets = [t for t in ctx.managed_targets if not is_windows_template(t["box"]["template"])]
    ctx.windows_targets = [t for t in ctx.managed_targets if is_windows_template(t["box"]["template"])]
