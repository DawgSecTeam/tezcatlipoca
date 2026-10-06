"""Golden slot arithmetic: vmids, IPs, template lookup, per-box target records."""

import json

from range_ops import cluster_vms_for, proxmox_api
from nodes_ops import golden_vmid_for_slot
from ssh_ops import quote_sshkeys
from utils import is_unmanaged


# Single definition lives in ssh_ops (redeploy rebuilds use it too).
_quote_sshkeys = quote_sshkeys


def golden_vmid_for(engine_vmid, box_idx):
    """Golden boxes sit just above the engine's slot; preflight gates the span for
    collisions with team vmid space (engine vmids above ~1060 shift the golden block).
    Slot 0 of golden_vmid_for_slot — the satellite slots shift by box-stride (nodes_ops)."""
    return golden_vmid_for_slot(engine_vmid, 0, box_idx)


def golden_ip_for(anchor_identifier, box_idx):
    """Above the .1 gateway, below .255, on the anchor team's subnet — free because
    golden boxes are converted to (stopped) templates before any real team box
    exists. Satellite slots anchor on their first local team (the jump answers .1
    there and routes to the engine)."""
    return f"192.168.{anchor_identifier}.{240 + box_idx}"


def _is_template(node, vmid):
    try:
        return bool(proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"].get("template"))
    except Exception:
        return False


def _vm_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    return any(v["vmid"] == vmid for v in vms)


def _template_vmid_map(node):
    """name -> vmid for stopped VMs tagged `template`, ON `node` (mirrors main.tf's
    per-slot template data sources). Asks the host that owns `node`
    (range_ops.cluster_vms_for): with a multi-node placement that is the satellite's
    own endpoint — a primary-side cluster query would never see its templates on
    independent hosts. /cluster/resources joins tags with ';' (config GET uses ','),
    so split on both."""
    vms = cluster_vms_for(node)
    out = {}
    for vm in vms:
        if vm.get("node") and vm["node"] != node:
            continue
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if "template" in tags and vm.get("status") == "stopped":
            out[vm["name"]] = vm["vmid"]
    return out


def unbooted_golden_boxes(comp_dir):
    """Box types whose golden must stay sysprep-generalized (never booted): the
    domain-controller role in domain_roles.json. See the module identity note.

    An absent file is a no-domain lineup (empty set). A PRESENT but malformed or
    invalid file raises: treating it as no-domain would hand the DC a booted shared
    golden — the exact duplicate-DomainSID failure this split exists to prevent."""
    path = comp_dir / "domain_roles.json"
    if not path.exists():
        return set()
    try:
        roles = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"  ERROR: {path} is unreadable/malformed ({str(e)[:80]}) — "
                         "cannot decide which goldens must stay unbooted")
    if not isinstance(roles, dict) or not all(
            isinstance(name, str) and isinstance(role, str)
            for name, role in roles.items()):
        raise SystemExit(
            f"  ERROR: {path} must map box names to 'dc' or 'member' strings")
    bad = {name: role for name, role in roles.items() if role not in ("dc", "member")}
    if bad:
        raise SystemExit(
            f"  ERROR: {path} has invalid role value(s): "
            + ", ".join(f"{name}={role!r}" for name, role in sorted(bad.items()))
            + " (expected 'dc' or 'member')")
    boxes_path = comp_dir / "boxes.json"
    if boxes_path.exists():
        names = {b["name"] for b in json.loads(boxes_path.read_text())}
        unknown = [name for name in roles if name not in names]
        if unknown:
            raise SystemExit(
                f"  ERROR: {path} names box(es) absent from boxes.json: "
                + ", ".join(sorted(unknown)))
    return {name for name, role in roles.items() if role == "dc"}


def golden_targets(engine_vmid, teams, boxes, slot=0, anchor_identifier=None):
    """One target per box type: vmid/IP/name pre-derived, box_idx positional (mirrors
    enumerate_targets' invariant — build from the full box list, never a filtered one).

    slot 0 anchors on team1's subnet (historical behavior exactly); a satellite slot
    anchors on its first local team's subnet — the satellite's goldens must sit on a
    bridge that exists on the satellite's host, with the jump answering .1 there."""
    if anchor_identifier is None:
        anchor_identifier = str(teams["team1"]["identifier"])
    return [
        {
            "box": box,
            "box_idx": box_idx,
            "vmid": golden_vmid_for_slot(engine_vmid, slot, box_idx),
            "ip": golden_ip_for(anchor_identifier, box_idx),
            "vm_name": f"golden-{box['name']}",
            "machine": f"{box['name']}-golden",
            "gateway": f"192.168.{anchor_identifier}.1",
            "bridge": f"vmbr{anchor_identifier}",
        }
        for box_idx, box in enumerate(boxes)
        if not is_unmanaged(box)  # no golden for firewall/appliance boxes; box_idx stays positional
    ]


# ------------------------------------------------------------------ boot smoke gate
#
# The invariant "no boot-hostile config rides the golden" was, until now, enforced by a
# comment and the hand-maintained constants.FINAL_STAGE_CONFIGS set — nothing else. It
# already failed for real on 2026-09-24: `systemd-system-masked` was planted into the
# golden disk and every linked clone was bootless (masked multi-user/graphical/default
# targets => systemd boots with no multi-user.target => no cloud-init, no network; see
# docs/benchmark-m02.md and the constants.py note). strict=True does not catch it: the
# plant returns rc=0 while the disk is unbootable, and the golden's own running system
# stays reachable (masking a target does not stop the current boot), so the damage only
# surfaces one phase later, on every team clone at once. Hence a real boot of a
# throwaway clone of the exact disk about to be sealed.
