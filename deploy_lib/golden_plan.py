"""Golden-template planning: per-box hashes, and the rebuild gate they drive.

golden_hash_entries computes the hashes before phase 1; golden_rebuild_gate (phase 4's
per-slot rebuild gate) consumes them. The gate lives here so the tag-ownership +
frozen-gate rules that make a template destroy safe are read in one place. Phase 1 no
longer destroys anything a run owns — a leftover range is a preflight refusal
(infra.collisions) reclaimed by destroy-competition.py.
"""

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
# golden_inputs/golden_hashes for every box in boxes.json — unmanaged included.
#
#     Extracted from deploy() (the pure part of the M4 hash loop) so the all-boxes
#     invariant is testable offline: the phase-4 rebuild gate and `.template-hashes.json`
#     both key off these dicts, and both iterate the FULL lineup, not just the planted
#     boxes.
#
#     Every box MUST get an entry. The unmanaged (pfSense/appliance) case is the one that
#     used to be unreachable: 8cb755c3 (2026-09-30) added an early `continue` ABOVE
#     e91a5039's compensating unmanaged-inputs block, so the block went dead and the slot
#     carried no hash. The merge-order accident did not fix the KeyError, it moved it into
#     the phase-4 gate (live-found 2026-09-29, cyberfield hdrives-zfs: the first
#     pfsense-carried bundle through the M4 hash path). Unmanaged is checked FIRST now so
#     the entry exists no matter where a later branch continues from.
    golden_inputs, golden_hashes = {}, {}
    for b in boxes:
        if is_unmanaged(b):
            # No golden machine, no golden plant: terraform clones it straight from
            # its own base template. The slot still needs a stable hash entry so the
            # rebuild gate never subscripts it into a KeyError.
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
    """True when `vmid` holds a stopped template that is THIS competition's own golden.

    The proof is the same one infra.collisions makes for a template slot: template=1 + our comp
    tag + the golden marker tag. Anything else (a foreign template, a running VM) is not ours to
    adopt and stays refused.
    """
    from infra.client import proxmox_api
    cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config")["data"]
    if cfg.get("template") != 1:
        return False
    tags = {t.strip() for t in str(cfg.get("tags") or "").replace(";", ",").split(",") if t.strip()}
    return f"comp-{comp_name}" in tags and GOLDEN_TAG in tags


def golden_rebuild_gate(comp_dir, destroy_node, slot, boxes, engine_vmid, stored,
                        golden_hashes, golden_inputs, comp_name, run_tag=None):
# Destroy each golden template in `slot` whose stored M4 hash no longer matches.
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
            # fresh (dashboard-seeded) workspace has no prior state to reclaim it by: the reclaim
            # tag falls back to this run's id, which no existing VM has — so the rebuild's own
            # ownership proof refused our own cached template and killed phase 4
            # (run-27b0d9e4, 2026-10-14: "refusing to destroy vmid 1151 (golden-win02) ... missing
            # ['run-a48d4f5b']"). Adopt it first, under the template-slot proof the preflight
            # already makes, exactly as phase 2 re-stamps a REUSED engine template.
            if _is_own_golden_template(destroy_node, vid, comp_name):
                retag_ownership(destroy_node, vid, ownership)
            destroy_vm_if_exists(destroy_node, vid, expect_tags=ownership)
