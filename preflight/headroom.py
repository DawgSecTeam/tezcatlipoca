"""Gate: datastore headroom (provisioned team-disk math vs pool avail)."""

import os

from pve_api import proxmox_api


def check_datastore_headroom(node, datastore, boxes, num_teams, label="", node_name=None):
    """Blocking datastore-headroom gate: provisioned team-disk math vs pool avail.

    `label` ("[name]") and `node_name` are the multi-node scoping: the same gate run once per
    hosting node over that node's share."""
    # The storage LIST zeroes/omits free on some pools (cyberfield hdrives-zfs);
    # the per-store STATUS endpoint's avail is the authoritative number.
    try:
        st = proxmox_api("GET", f"/nodes/{node}/storage/{datastore}/status")["data"]
        free = st.get("avail")
    except Exception:
        free = None
    if free is None:
        print(f"  WARNING: could not read free space on datastore '{datastore}' — "
              f"headroom unchecked" if not label else
              f"  Preflight{label}: WARNING could not read free space on "
              f"'{datastore}' — headroom unchecked")
        return
    free_gb = free / 1024 ** 3
    # disk_gb unset means "template's own disk", unknowable here; 40 GB is a
    # conservative stand-in across the base templates.
    need_gb = num_teams * sum(b.get("disk_gb") or 40 for b in boxes)
    # On thin-provisioned pools (ZFS, lvmthin) linked clones only allocate written
    # blocks — the goldens and engine are the only full copies. Opt-in factor
    # (0 < TEZ_THIN_HEADROOM <= 1) counts that fraction of the provisioned math
    # instead of rejecting comps the pool can actually hold.
    thin = float(os.environ.get("TEZ_THIN_HEADROOM") or 1.0)
    if not 0 < thin <= 1:
        raise SystemExit(f"  ERROR: TEZ_THIN_HEADROOM must be in (0, 1], got {thin}")
    counted_gb = need_gb * thin
    on = f" on '{node_name}'" if node_name else ""
    if free_gb < counted_gb:
        raise SystemExit(
            f"  ERROR: datastore '{datastore}'{on} has {free_gb:.0f} GB free; this deploy "
            f"needs ~{need_gb:.0f} GB ({num_teams} teams x {len(boxes)} boxes, unset disk "
            f"sizes counted as 40 GB"
            + (f", x{thin} thin factor" if thin != 1.0 else "")
            + "). Free space or trim the competition first.")
    print(f"  Preflight{label}: datastore '{datastore}' {free_gb:.0f} GB free vs "
          f"~{counted_gb:.0f} GB needed"
          + (f" (provisioned ~{need_gb:.0f} GB x{thin} thin)" if thin != 1.0 else ""))
