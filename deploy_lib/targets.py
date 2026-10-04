"""Enumerate every VM the deploy touches and split them into the phases' work lists."""

import os

from range_ops import enumerate_targets, persist_targets
from utils import is_unmanaged
from windows_ops import is_windows_template


def enumerate_deploy_targets(targets, comp_dir, spec, secrets, place):
    """Enumerate every VM this deploy will touch, into `targets`.

    node is read from the env here (the engine's host, already selected by placement);
    all_targets keeps every positional vmid, while the Linux/Windows work lists drop
    unmanaged boxes — pfSense/appliances have no plant/repair/fix_services/cloud-init,
    they clone from their own template and self-configure (but must stay in all_targets
    for vmid arithmetic)."""
    targets.node = os.environ["TF_VAR_proxmox_node"]
    targets.all_targets = enumerate_targets(secrets.teams, spec.boxes,
                                           placement=place.placement, default_node=targets.node)
    persist_targets(comp_dir, targets.all_targets, spec.boxes)
    # Unmanaged boxes (pfSense/appliances) get no plant/repair/fix_services/cloud-init —
    # they are cloned from their own template and self-configure. Keep them in all_targets
    # (positional vmids) but out of the Linux/Windows work lists.
    targets.managed_targets = [t for t in targets.all_targets if not is_unmanaged(t["box"])]
    targets.linux_targets = [t for t in targets.managed_targets if not is_windows_template(t["box"]["template"])]
    targets.windows_targets = [t for t in targets.managed_targets if is_windows_template(t["box"]["template"])]
