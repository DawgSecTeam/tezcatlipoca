"""Golden slot arithmetic: vmids, IPs, template lookup, per-box target records."""

import json

from common.constants import GOLDEN_IP_BASE
from infra.client import cluster_vms_for, proxmox_api
from multinode.placement_record import golden_vmid_for_slot
from common.ssh_ops import quote_sshkeys
from common.utils import is_unmanaged


# Single definition lives in ssh_ops (redeploy rebuilds use it too).
_quote_sshkeys = quote_sshkeys


# Slot 0 of golden_vmid_for_slot (satellite slots shift by box-stride — placement_record).
# Golden boxes sit just above the engine's slot; preflight gates the span for
# collisions with team vmid space.
def golden_vmid_for(engine_vmid, box_idx):
    return golden_vmid_for_slot(engine_vmid, 0, box_idx)


# Free because golden boxes are converted to (stopped) templates before any real team
# box exists. Satellite slots anchor on their first local team (the jump answers .1
# there and routes to the engine).
def golden_ip_for(anchor_identifier, box_idx):
    return f"192.168.{anchor_identifier}.{GOLDEN_IP_BASE + box_idx}"


def _is_template(node, vmid):
    try:
        return bool(proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"].get("template"))
    except Exception:
        return False


def _vm_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    return any(v["vmid"] == vmid for v in vms)


# Asks the host that owns `node` (infra.client.cluster_vms_for): with a multi-node
# placement that is the satellite's own endpoint — a primary-side cluster query would
# never see its templates on independent hosts.
def _template_vmid_map(node):
    vms = cluster_vms_for(node)
    out = {}
    for vm in vms:
        if vm.get("node") and vm["node"] != node:
            continue
        # /cluster/resources joins tags with ';' (config GET uses ','), so split on both.
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if "template" in tags and vm.get("status") == "stopped":
            out[vm["name"]] = vm["vmid"]
    return out


# A PRESENT but malformed or invalid domain_roles.json raises: treating it as
# no-domain would hand the DC a booted shared golden — the exact duplicate-DomainSID
# failure this split exists to prevent.
def unbooted_golden_boxes(comp_dir):
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
    names = {b["name"] for b in json.loads((comp_dir / "boxes.json").read_text())}
    unknown = [name for name in roles if name not in names]
    if unknown:
        raise SystemExit(
            f"  ERROR: {path} names box(es) absent from boxes.json: "
            + ", ".join(sorted(unknown)))
    return {name for name, role in roles.items() if role == "dc"}


# box_idx stays positional — mirrors enumerate_targets' invariant: build from the full
# box list, never a filtered one. Slot 0 anchors on team1's subnet (historical
# behavior exactly); a satellite slot anchors on its first local team's subnet — the
# satellite's goldens must sit on a bridge that exists on the satellite's host, with
# the jump answering .1 there.
def golden_targets(engine_vmid, teams, boxes, slot=0, anchor_identifier=None):
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
