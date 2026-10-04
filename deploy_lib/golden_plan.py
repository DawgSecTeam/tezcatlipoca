"""Golden-template planning: per-box hashes, and the destroy/keep decisions they drive.

golden_hash_entries computes the hashes before phase 1; phase1_destroy_waves (phase 1's
teardown decision) and golden_rebuild_gate (phase 4's per-slot rebuild gate) both consume
them. Both destroy decisions live here so the tag-ownership + frozen-gate rules that make
a destroy safe are read in one file.
"""

from constants import GOLDEN_TAG, MAX_BOXES_PER_TEAM, ownership_tags
from golden_ops import _is_template, build_golden_set
from hardening_ops import _APT_PREP_BODY, _apt_prep_script
from nodes_ops import golden_vmid_for_slot
from range_ops import destroy_vm_if_exists
from template_ops import (code_hash, engine_template_vmid, frozen_gate,
                          golden_hash_inputs, golden_payload_hash,
                          hash_from_inputs, stored_template_hash)
from utils import is_unmanaged


def golden_hash_entries(boxes, base_template_ids, golden_machines_by_box, golden_bundle,
                        box_password, box_username, ssh_public_key, apt_cache, unbooted=()):
    """golden_inputs/golden_hashes for every box in boxes.json — unmanaged included.

    Extracted from deploy() (the pure part of the M4 hash loop) so the all-boxes
    invariant is testable offline: phase 1's wave logic, the phase-4 rebuild gate and
    `.template-hashes.json` all key off these dicts, and all three iterate the FULL
    lineup, not just the planted boxes.

    Every box MUST get an entry. The unmanaged (pfSense/appliance) case is the one that
    used to be unreachable: 8cb755c3 (2026-09-30) added an early `continue` ABOVE
    e91a5039's compensating unmanaged-inputs block, so the block went dead and the slot
    carried no hash. The merge-order accident did not fix the KeyError, it moved it into
    the phase-4 gate (live-found 2026-09-29, cyberfield hdrives-zfs: the first
    pfsense-carried bundle through the M4 hash path). Unmanaged is checked FIRST now so
    the entry exists no matter where a later branch continues from."""
    golden_inputs, golden_hashes = {}, {}
    for b in boxes:
        if is_unmanaged(b):
            # No golden machine, no golden plant: terraform clones it straight from
            # its own base template. The slot still needs a stable hash entry so the
            # rebuild gate and phase 1's wave logic never subscript it into a KeyError.
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


def golden_rebuild_gate(comp_dir, destroy_node, slot, boxes, engine_vmid, stored,
                        golden_hashes, golden_inputs, comp_name, run_tag=None):
    """Destroy each golden template in `slot` whose stored M4 hash no longer matches.

    A slot with no hash/inputs entry is SKIPPED, not subscripted. The unconditional
    `golden_hashes[b["name"]]` here raised KeyError out of phase 4 for an
    unmanaged-carried lineup (whose box had no entry — see golden_hash_entries) on a
    --from-phase 4 resume or a box flipped to `unmanaged: true` under a live golden,
    the exact case deploy() anticipates when it maps an unmanaged slot to its base
    template (live-found 2026-09-29, cyberfield hdrives-zfs). With no computed hash
    there is nothing to compare the stored one against, so skipping is correct; the
    old code moved the KeyError here instead of fixing it."""
    for i, b in enumerate(boxes):
        vid = golden_vmid_for_slot(engine_vmid, slot, i)
        if not _is_template(destroy_node, vid):
            continue
        box_hash = golden_hashes.get(b["name"])
        box_inputs = golden_inputs.get(b["name"])
        if box_hash is None or box_inputs is None:
            continue
        if stored_template_hash(destroy_node, vid) == box_hash:
            continue
        entry = stored.get("golden", {}).get(b["name"]) or {}
        if frozen_gate(comp_dir, entry.get("inputs"), box_inputs,
                       f"golden template for '{b['name']}'"):
            print(f"  golden-{b['name']} hash differs — rebuilding...")
            destroy_vm_if_exists(destroy_node, vid, expect_tags=ownership_tags(
                comp_name, run_tag, GOLDEN_TAG))


def phase1_destroy_waves(node_vms, all_targets, engine_vmid, boxes, comp_tags,
                         is_template, stored_hashes, golden_hashes, frozen_keep=(), slot=0,
                         extra_destroy=None):
    """Phase 1's teardown decision, pure so the teardown→redeploy loop is testable offline.

    Wave 1: every team box (the computed set covers ALL teams now that terraform builds
    them) and stranded clones from a PREVIOUS
    run with more teams — comp-tagged team boxes the current (smaller) team set no longer
    enumerates. Left behind they keep a linked-clone hold on the goldens, so wave 2 can't
    rebuild them (winad-testrun 2026-09-25: 3-team run then 2-team redeploy). Linked
    clones must die BEFORE their templates.
    Wave 2: the engine (a linked clone of the engine template — it dies and re-clones
    cheaply every run; slot 0 only), then every golden template that is missing,
    hash-mismatched, or a stale slot beyond the current lineup. MATCHING golden templates
    survive — that is the whole test-run reuse (build once per competition, reuse across
    its test runs). A frozen competition's code-only-drifted goldens (names in
    frozen_keep, as classified by the pre-phase-1 golden_freeze_gate) also survive: the
    gate already said "proceeding on the frozen template", so destroying the golden here
    would rebuild it and silently break freeze semantics (internals "frozen-gate golden
    keep").
    slot>0 (multi-node satellite): this node's golden span (slot-shifted vmids), no
    engine, plus extra_destroy (the satellite's jump VM) — the caller runs one wave
    pair per hosting node with that node's own targets."""
    wave1 = {t["vmid"]: t["vm_name"] for t in all_targets}
    n_slots = max(len(boxes), MAX_BOXES_PER_TEAM)
    keep_vmids = {golden_vmid_for_slot(engine_vmid, slot, i) for i in range(n_slots)}
    if slot == 0:
        keep_vmids |= {engine_vmid, engine_template_vmid(engine_vmid)}
    for vm in node_vms:
        vid = vm["vmid"]
        if vid in wave1 or vid in keep_vmids:
            continue
        raw = str(vm.get("tags") or "")
        tags = {t.strip() for t in raw.replace(";", ",").split(",") if t.strip()}
        if comp_tags <= tags:
            wave1[vid] = f"{vm.get('name') or vid} (stranded clone)"
    wave2 = {engine_vmid: f"engine-{engine_vmid}"} if slot == 0 else {}
    for i in range(n_slots):
        vid = golden_vmid_for_slot(engine_vmid, slot, i)
        b = boxes[i] if i < len(boxes) else None
        if b is None:
            wave2[vid] = f"golden-slot-{i} (stale)"
            continue
        if is_unmanaged(b):
            # Unmanaged boxes have no golden; anything templated in the slot is a
            # stale leftover from an earlier lineup and nothing references it.
            wave2[vid] = f"golden-slot-{i} (stale, unmanaged box)"
            continue
        if (is_template(vid)
                and stored_hashes.get("golden", {}).get(b["name"], {}).get("hash")
                == golden_hashes[b["name"]]):
            print(f"  golden-{b['name']} hash matches — keeping template (test-run reuse)")
            continue
        if b["name"] in frozen_keep and is_template(vid):
            print(f"  golden-{b['name']} frozen with code-only drift — keeping the frozen "
                  f"template (rebuild would break freeze semantics)")
            continue
        wave2[vid] = f"golden-{b['name']}"
    if extra_destroy:
        wave2.update(extra_destroy)
    return wave1, wave2
