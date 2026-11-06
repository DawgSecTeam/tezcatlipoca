"""Enumerate every VM the deploy touches and split them into the phases' work lists."""


import providers

from common.targets import enumerate_targets, persist_targets
from common.utils import is_unmanaged
from common.windows_ops import is_windows_template
from estate import config


def enumerate_deploy_targets(ctx, comp_dir):
# Enumerate every VM this run will touch, onto `ctx`.
#
#     node is the hosting scope on the roster (the engine's PVE host, or the gcp zone —
#     everything on one platform lives inside one zone per range); all_targets keeps
#     every positional vmid, while the Linux/Windows work lists drop unmanaged boxes —
#     pfSense/appliances have no plant/pre-domain/fix_services/cloud-init, they clone
#     from their own template and self-configure (but must stay in all_targets
#     for vmid arithmetic).
    if ctx.platform == providers.GCP:
        ctx.node = config.active().gcp.zone
    else:
        ctx.node = config.active().proxmox.node
    ctx.all_targets = enumerate_targets(ctx.teams, ctx.boxes,
                                           placement=ctx.placement, default_node=ctx.node)
    persist_targets(comp_dir, ctx.all_targets, ctx.boxes)
    # Unmanaged boxes (pfSense/appliances) get no plant/pre-domain/fix_services/cloud-init —
    # they are cloned from their own template and self-configure. Keep them in all_targets
    # (positional vmids) but out of the Linux/Windows work lists.
    ctx.managed_targets = [t for t in ctx.all_targets if not is_unmanaged(t["box"])]
    ctx.linux_targets = [t for t in ctx.managed_targets if not is_windows_template(t["box"]["template"])]
    ctx.windows_targets = [t for t in ctx.managed_targets if is_windows_template(t["box"]["template"])]
