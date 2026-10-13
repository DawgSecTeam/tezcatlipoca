"""Enumerate every VM the deploy touches and split them into the phases' work lists."""

import os

from common.targets import enumerate_targets, persist_targets
from common.utils import is_unmanaged
from common.windows_ops import is_windows_template


def enumerate_deploy_targets(ctx, comp_dir):
# Enumerate every VM this deploy will touch, onto `ctx`.
#
#     node is read from the env here (the engine's host, already selected by placement);
#     all_targets keeps every positional vmid, while the Linux/Windows work lists drop
#     unmanaged boxes — pfSense/appliances have no plant/repair/fix_services/cloud-init,
#     they clone from their own template and self-configure (but must stay in all_targets
#     for vmid arithmetic).
    ctx.node = os.environ["TF_VAR_proxmox_node"]
    ctx.all_targets = enumerate_targets(ctx.teams, ctx.boxes,
                                           placement=ctx.placement, default_node=ctx.node)
    persist_targets(comp_dir, ctx.all_targets, ctx.boxes)
    # Unmanaged boxes (pfSense/appliances) get no plant/repair/fix_services/cloud-init —
    # they are cloned from their own template and self-configure. Keep them in all_targets
    # (positional vmids) but out of the Linux/Windows work lists.
    ctx.managed_targets = [t for t in ctx.all_targets if not is_unmanaged(t["box"])]
    ctx.linux_targets = [t for t in ctx.managed_targets if not is_windows_template(t["box"]["template"])]
    ctx.windows_targets = [t for t in ctx.managed_targets if is_windows_template(t["box"]["template"])]
