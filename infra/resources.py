"""Resource gates: the node must have what this deploy needs before it starts cloning."""

import re

from infra.client import proxmox_api
from estate import config

_CLOUDINIT_DISK_RE = re.compile(r"^(?:ide|scsi|sata)\d+$")

# Same numbers the creators size those VMs to (estate/apply.py, red/red01_ops.py).
ENGINE_TEMPLATE_GB = 40
RED01_DISK_GB = 60


def check_datastore_headroom(node, datastore, boxes, num_teams, label="", node_name=None):
    # The storage LIST zeroes/omits free on some pools; the per-store STATUS avail is the
    # authoritative number.
    free = proxmox_api("GET", f"/nodes/{node}/storage/{datastore}/status")["data"].get("avail")
    if free is None:
        print(f"  WARNING: could not read free space on datastore '{datastore}' — "
              f"headroom unchecked" if not label else
              f"  Preflight{label}: WARNING could not read free space on "
              f"'{datastore}' — headroom unchecked")
        return
    free_gb = free / 1024 ** 3
    # disk_gb unset means "template's own disk", unknowable here; 40 GB is a conservative
    # stand-in across the base templates.
    team_gb = num_teams * sum(b.get("disk_gb") or 40 for b in boxes)
    # Full-clone artifacts that live on the SAME datastore and are not thin: one golden per
    # box type, the engine template, the engine itself and red01. Omitting them made a small
    # range pass the gate and then fill the pool mid-deploy.
    golden_gb = sum(b.get("disk_gb") or 40 for b in boxes)
    fixed_gb = golden_gb + ENGINE_TEMPLATE_GB + ENGINE_TEMPLATE_GB + RED01_DISK_GB
    need_gb = team_gb + fixed_gb
    # Thin-provisioned pools only allocate written blocks, so TEZ_THIN_HEADROOM scales down
    # the provisioned math.
    thin = config.active().knobs.thin_headroom
    if not 0 < thin <= 1:
        raise SystemExit(f"  ERROR: TEZ_THIN_HEADROOM must be in (0, 1], got {thin}")
    # Only the team clones are thin-provisioned; the full-clone artifacts are not.
    counted_gb = team_gb * thin + fixed_gb
    on = f" on '{node_name}'" if node_name else ""
    if free_gb < counted_gb:
        raise SystemExit(
            f"  ERROR: datastore '{datastore}'{on} has {free_gb:.0f} GB free; this deploy "
            f"needs ~{counted_gb:.0f} GB ({num_teams} teams x {len(boxes)} boxes, unset disk "
            f"sizes counted as 40 GB, plus the golden set, engine template, engine and "
            f"red01"
            + (f"; team clones x{thin} thin factor" if thin != 1.0 else "")
            + "). Free space or trim the competition first.")
    print(f"  Preflight{label}: datastore '{datastore}' {free_gb:.0f} GB free vs "
          f"~{counted_gb:.0f} GB needed"
          + (f" (provisioned ~{need_gb:.0f} GB x{thin} thin)" if thin != 1.0 else ""))


def template_cloudinit_missing(config):
    ostype = str((config or {}).get("ostype") or "").strip().lower()
    if not ostype.startswith("l"):
        return False
    for key, value in (config or {}).items():
        if _CLOUDINIT_DISK_RE.match(str(key)) and "cloudinit" in str(value).lower():
            return False
    return True


def cloudinit_gate(tagged_by_name, boxes, label=""):
    seen = set()
    verified = 0
    for box in boxes:
        if box.get("unmanaged"):
            continue  # unmanaged boxes get no cloud-init identity (the create path skips the block)
        name = box.get("template")
        vm = tagged_by_name.get(name)
        if vm is None or name in seen:
            continue
        seen.add(name)
        vmid, node = vm.get("vmid"), vm.get("node")
        try:
            config = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
        except Exception as e:
            print(f"  WARNING: could not read template '{name}' (vmid {vmid}) config — its "
                  f"cloud-init drive is UNVERIFIED ({str(e)[:80]})")
            continue
        if template_cloudinit_missing(config):
            fix = name if str(name).endswith("-fix") else f"{name}-fix"
            raise SystemExit(
                f"  ERROR: box template '{name}' (vmid {vmid}, node {node}, ostype "
                f"{config.get('ostype') or '?'}) is Linux but has no cloud-init drive — "
                f"its linked clones would boot with no network or identity and fail an "
                f"hour later at wait_for_boxes_ssh. Use a cloud-init-capable template "
                f"(e.g. the '{fix}' variant) or add a cloud-init drive to it.")
        verified += 1
    if verified:
        print(f"  Preflight{label}: {verified} box template(s) carry a cloud-init drive")
    return verified


def tagged_templates(vms):
    tagged = set()
    by_name = {}
    for vm in vms:
        if vm.get("template") == 1 and "template" in (vm.get("tags") or "").split(";"):
            tagged.add(vm.get("name"))
            by_name.setdefault(vm.get("name"), vm)
    return tagged, by_name


def check_share_templates(share, boxes, vms):
    # A clone can only use templates that exist on its own node.
    where = f"node '{share.name}' ({share.node})" if share.multi else "the cluster"
    if share.check_templates:
        tagged, tagged_by_name = tagged_templates(vms)
        missing = sorted({b["template"] for b in boxes} - tagged)
        if missing:
            if share.multi:
                raise SystemExit(
                    f"  ERROR: box template(s) with no tagged template on {where}: "
                    + ", ".join(missing)
                    + f". Available there: {sorted(t for t in tagged if t) or '(none)'}. "
                      "Sync with sync-template.py or move the teams.")
            raise SystemExit(
                "  ERROR: box template(s) with no tagged template on the cluster: "
                + ", ".join(missing)
                + ". Clones would fail mid-apply; available: "
                + (", ".join(sorted(t for t in tagged if t)) or "(none)"))
        cloudinit_gate(tagged_by_name, boxes, label=share.label)
    base = share.engine_base() if share.engine_base else None
    if base is not None and not any(vm.get("vmid") == base for vm in vms):
        if share.multi:
            raise SystemExit(
                f"  ERROR: engine base image vmid {base} does not exist on "
                f"engine node '{share.name}' ({share.node}) — the engine-template build would "
                f"fail.")
        raise SystemExit(
            f"  ERROR: engine base image vmid {base} (TEZ_PROXMOX_TEMPLATE_VM_ID) "
            f"does not exist on this cluster — the engine-template build and the scoring "
            f"engine clone would fail mid-apply.")
    if share.needs_jump_template:
        from net.jump_ops import find_jump_template
        find_jump_template(share.node, share.jump_template)
        print(f"  Preflight{share.label}: box templates + jump clone source present")
    elif not share.multi:
        print(f"  Preflight: all {len(boxes)} box template(s) resolve; engine base image vmid "
              f"{base} present")
