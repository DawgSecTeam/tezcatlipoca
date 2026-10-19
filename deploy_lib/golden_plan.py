"""Golden-template planning: per-box hashes, and the phase-4 rebuild gate they drive."""

from common.constants import GOLDEN_TAG, ownership_tags
from golden.golden_target_ops import _is_template
from golden.golden_ops import build_golden_set
from guests.apt_dns_ops import _APT_PREP_BODY, _apt_prep_script
from multinode.placement_record import golden_vmid_for_slot
from infra.ownership import destroy_vm_if_exists, retag_ownership
from engine.template_freeze import (
    code_hash, frozen_gate, golden_hash_inputs, golden_payload_hash, hash_from_inputs,
    template_stamp_current,
)
from common.utils import is_unmanaged


def golden_hash_entries(boxes, base_template_ids, golden_machines_by_box, golden_bundle,
                        box_password, box_username, ssh_public_key, apt_cache, unbooted=()):
# golden_inputs/golden_hashes for every box, unmanaged included — the phase-4 rebuild
# gate and the template hash record both iterate the FULL lineup, so every box MUST get
# an entry; unmanaged is checked first so a later `continue` can never leave the slot
# unhashed.
    golden_inputs, golden_hashes = {}, {}
    for b in boxes:
        if is_unmanaged(b):
            # No golden machine, no golden plant: the box is cloned straight from its own
            # base template, but the slot still needs a stable hash entry.
            inputs = {"config": {"base_template_vmid": base_template_ids.get(b["template"]),
                                 "disk_gb": b.get("disk_gb"), "golden": "unmanaged"},
                      "code": {"build_golden_set": code_hash(build_golden_set)}}
            golden_inputs[b["name"]] = inputs
            golden_hashes[b["name"]] = hash_from_inputs(inputs)
            continue
        if b["name"] in unbooted:
            inputs = {"config": {"base_template_vmid": base_template_ids.get(b["template"]),
                                  "disk_gb": b.get("disk_gb"), "golden": "unbooted"},
                      "code": {"build_golden_set": code_hash(build_golden_set)}}
            golden_inputs[b["name"]] = inputs
            golden_hashes[b["name"]] = hash_from_inputs(inputs)
            continue
        inputs = golden_hash_inputs(
            b, golden_machines_by_box[b["name"]],
            golden_payload_hash(golden_bundle, b["name"]),
            box_password, box_username, ssh_public_key, apt_cache)
        inputs["config"]["base_template_vmid"] = base_template_ids.get(b["template"])
        inputs["code"]["build_golden_set+apt_prep"] = code_hash(
            build_golden_set, _APT_PREP_BODY, _apt_prep_script)
        golden_inputs[b["name"]] = inputs
        golden_hashes[b["name"]] = hash_from_inputs(inputs)
    return golden_inputs, golden_hashes


def _is_own_golden_template(node, vmid, comp_name):
    """The proof is the one infra.collisions makes for a template slot: template=1 + our
    comp tag + the golden marker tag; anything else stays refused."""
    from infra.client import proxmox_api
    try:
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    except Exception:
        # Unreadable config = nothing adopted; the destroy then refuses on the ownership
        # proof rather than acting on a guess.
        return False
    if cfg.get("template") != 1:
        return False
    tags = {t.strip() for t in str(cfg.get("tags") or "").replace(";", ",").split(",") if t.strip()}
    return f"comp-{comp_name}" in tags and GOLDEN_TAG in tags


def golden_rebuild_gate(comp_dir, destroy_node, slot, boxes, engine_vmid, stored,
                        golden_hashes, golden_inputs, comp_name, run_tag=None):
    for i, b in enumerate(boxes):
        vid = golden_vmid_for_slot(engine_vmid, slot, i)
        if not _is_template(destroy_node, vid):
            continue
        box_hash = golden_hashes[b["name"]]
        if template_stamp_current(destroy_node, vid, box_hash):
            continue
        entry = stored.get("golden", {}).get(b["name"]) or {}
        if frozen_gate(comp_dir, entry.get("inputs"), golden_inputs[b["name"]],
                       f"golden template for '{b['name']}'"):
            print(f"  golden-{b['name']} hash differs — rebuilding...")
            ownership = ownership_tags(comp_name, run_tag, GOLDEN_TAG)
            # A golden kept by an EARLIER run of this competition carries THAT run's tag, and a
            # fresh (dashboard-seeded) workspace has no state to reclaim it by — the reclaim
            # tag falls back to this run's id, which no existing VM has, so the rebuild's own
            # ownership proof refuses our own cached template. Adopt it first, under the
            # template-slot proof the preflight already makes, as phase 2 re-stamps a reused
            # engine template.
            if _is_own_golden_template(destroy_node, vid, comp_name):
                retag_ownership(destroy_node, vid, ownership)
            destroy_vm_if_exists(destroy_node, vid, expect_tags=ownership)
