"""Advisory gate: managed boxes that carry no pins at all."""

import json


def unpinned_managed_boxes(comp_dir, boxes):
    """Names of managed boxes with no entry in box_services/box_vulns/box_baseline.
    Unreadable pin files yield [] — the catalog gate owns reporting those."""
    pinned = set()
    for fname in ("box_services.json", "box_vulns.json", "box_baseline.json"):
        try:
            data = json.loads((comp_dir / fname).read_text())
        except (OSError, ValueError):
            if fname != "box_baseline.json":
                return []
            continue
        pinned |= {box for box, pins in data.items() if pins}
    return [b["name"] for b in boxes if not b.get("unmanaged") and b["name"] not in pinned]


def warn_unpinned_boxes(comp_dir, boxes):
    """Non-fatal: a zero-pin box plants a 0-step plan, which is a clean no-op."""
    names = unpinned_managed_boxes(comp_dir, boxes)
    if names:
        print(f"  Preflight WARNING: managed box(es) with zero pins: {', '.join(names)} — "
              f"the golden will be pristine and strict passes reconcile the no-op")
    return names
