"""Generated nakon/stage configs and the golden hashes + frozen gate computed from them."""

import json
import os

from golden_ops import _template_vmid_map, unbooted_golden_boxes
from nakon_ops import build_nakon_bundle, generate_nakon_config, generate_stage_configs
from template_ops import frozen_code_drift, frozen_state, golden_freeze_gate

from deploy_lib.golden_plan import golden_hash_entries


def generate_stage_configs_and_hashes(generated, comp_dir, spec, secrets):
    """Write the nakon/stage configs and compute every golden hash, into `generated`.

    The M4 golden hashes are computed BEFORE phase 1 — cleanup must know which golden
    templates survive (test-run reuse) and which rebuild — and the frozen gate fires
    here for the same reason: a config-class freeze must be able to say "nothing
    destroyed" before phase 1 starts deleting. The golden bundle is built now (content
    addressed; the phase-4 plant reuses the cache) because each box's hash consumes its
    plan's payload shas. See the comments on each sub-block for the incident history."""
    generated.nakon_config_path = generate_nakon_config(secrets.teams, spec.boxes, spec.difficulty, comp_dir, secrets.box_password,
                                               box_username=spec.box_username)
    # M3.2: the full bundle is never deployed as one pass anymore. The golden-stage
    # bundle is built inside golden_ops at plant time; the repair bundle in phase 5 and
    # the final bundle in phase 6 (all content-addressed, so resumes hit the cache).
    # Domain controllers keep an unbooted golden so each team's forest specializes its
    # own machine SID before promotion; their configs move to the repair stage.
    generated.unbooted = unbooted_golden_boxes(comp_dir)
    generated.golden_config_path, generated.repair_config_path, generated.final_config_path, _postclone_path = generate_stage_configs(
        comp_dir, secrets.teams, spec.boxes, unbooted=generated.unbooted)

    # M4: template hashes are computed BEFORE phase 1 — cleanup must know which golden
    # templates survive (test-run reuse) and which rebuild. The golden bundle is built
    # now (content-addressed; the phase-4 plant reuses the cache) because each box's
    # hash consumes its plan's payload shas. The frozen gate also fires HERE — before
    # phase 1 destroys anything ("nothing destroyed" is the whole point of the
    # freeze; the phase-4 per-box gate alone was too late, matrix run 4).
    node = os.environ["TF_VAR_proxmox_node"]
    golden_bundle = build_nakon_bundle(generated.golden_config_path)
    golden_machines_by_box = {
        m["name"].rsplit("-golden", 1)[0]: m
        for m in json.loads(generated.golden_config_path.read_text())["machines"]
    }
    base_template_ids = _template_vmid_map(node)
    generated.golden_inputs, generated.golden_hashes = golden_hash_entries(
        spec.boxes, base_template_ids, golden_machines_by_box, golden_bundle,
        secrets.box_password, spec.box_username, os.environ.get("TF_VAR_ssh_public_key", ""),
        spec.apt_cache, unbooted=generated.unbooted)

    frozen = frozen_state(comp_dir)
    generated.frozen_keep = set()
    if frozen:
        # The frozen record's `code` (commit + dirty) was written but never read: the
        # per-template drift gate below keys on input hashes and its code-class verdict
        # is warn-only, so a commit after --freeze used to run silently on unverified
        # code. Warn once here, before the golden loop, naming both commits.
        code_warning = frozen_code_drift(frozen)
        if code_warning:
            print(code_warning)
        # Goldens now: config-class drift refuses BEFORE phase 1 destroys anything.
        # The engine's gate still runs at phase 2 (its inputs are computed there),
        # likewise before any engine destruction. Code-only drift keeps the golden:
        # phase1_destroy_waves must not rebuild what the gate said to proceed on.
        frozen_hashes = (frozen.get("hashes") or {})
        # `box_name`, NOT `spec.name`: this loop used to read `for spec.name in
        # generated.golden_hashes`, which assigned each golden box name onto the spec
        # and left the LAST box there. On any FROZEN competition that clobbered
        # spec.name (the Compfile event name) before its later uses, so terraform's
        # `event_name` — and therefore `local.comp_tag`, the ownership tag on the engine
        # and every team box — became "comp-web01" instead of "comp-<competition>".
        # Phase 1's destroy compares against `comp-<competition>`, fails its
        # `comp_tags <= tags` ownership check, and REFUSES to reclaim the range's own
        # VMs; confirm_deploy also showed the wrong name. Fail-safe rather than
        # destructive, but it strands infrastructure and the next deploy collides on
        # those vmids.
        for box_name in generated.golden_hashes:
            stored_inputs = (frozen_hashes.get("golden") or {}).get(box_name, {}).get("inputs") or {}
            drift = golden_freeze_gate(box_name, stored_inputs, generated.golden_inputs[box_name],
                                       frozen.get("frozen_at"), golden_bundle)
            if drift["code"]:
                generated.frozen_keep.add(box_name)
