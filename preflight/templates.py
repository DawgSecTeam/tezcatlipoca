"""Gates: box templates resolve (and carry cloud-init), engine base image exists, jump source."""

import re

from pve_api import proxmox_api

_CLOUDINIT_DISK_RE = re.compile(r"^(?:ide|scsi|sata)\d+$")


def template_cloudinit_missing(config):
    """True when a Linux-ostype VM config carries no cloud-init drive.

    A template can be tagged `template` and still be cloud-init-less — .150 vmid 920 is
    literally named `base-debian13-cloudinit` and ships none — so its linked clones boot
    with no network and no identity and the failure only surfaces an hour later at
    wait_for_boxes_ssh. Windows ostypes never carry one (identity comes from
    bootstrap_windows_box), so they and every other non-Linux ostype are exempt."""
    ostype = str((config or {}).get("ostype") or "").strip().lower()
    if not ostype.startswith("l"):
        return False
    for key, value in (config or {}).items():
        if _CLOUDINIT_DISK_RE.match(str(key)) and "cloudinit" in str(value).lower():
            return False
    return True


def cloudinit_gate(tagged_by_name, boxes, label=""):
    """Refuse a selected Linux template that has no cloud-init drive.

    `tagged_by_name` maps template name -> cluster-resource entry (vmid + node). A config
    read that fails is warned about, not fatal — a transient API error must not block a
    deploy, but the template's cloud-init drive stays marked UNVERIFIED. Returns the
    number of distinct templates whose config was actually read and checked."""
    seen = set()
    verified = 0
    for box in boxes:
        if box.get("unmanaged"):
            continue  # unmanaged boxes get no cloud-init identity (terraform skips the block)
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
    """(names, name -> cluster entry) of VMs that are templates AND carry the `template` tag."""
    tagged = set()
    by_name = {}
    for vm in vms:
        if vm.get("template") == 1 and "template" in (vm.get("tags") or "").split(";"):
            tagged.add(vm.get("name"))
            by_name.setdefault(vm.get("name"), vm)
    return tagged, by_name


def check_share_templates(share, boxes, vms):
    """Template resolution for one NodeShare (a clone can only use its own node's templates).

    Single-node: every box template on the cluster + the engine base image
    (TF_VAR_template_vm_id). Multi-node: engine base on the engine node, every box template on a
    node hosting teams, and a jump clone source on satellites. `vms` is the share's VM view."""
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
            f"  ERROR: engine base image vmid {base} (TF_VAR_template_vm_id) "
            f"does not exist on this cluster — the engine-template build and the scoring "
            f"engine clone would fail mid-apply.")
    if share.needs_jump_template:
        from jump_ops import find_jump_template
        find_jump_template(share.node, share.jump_template)  # raises with remedy
        print(f"  Preflight{share.label}: box templates + jump clone source present")
    elif not share.multi:
        print(f"  Preflight: all {len(boxes)} box template(s) resolve; engine base image vmid "
              f"{base} present")
