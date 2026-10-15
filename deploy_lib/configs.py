"""Generated nakon/stage configs and the golden hashes + frozen gate computed from them."""

import json

from golden.golden_target_ops import _template_vmid_map, unbooted_golden_boxes
from nakonops.nakon_bundle_ops import build_nakon_bundle
from nakonops.nakon_config_ops import generate_nakon_config, generate_stage_configs
from engine.template_freeze import frozen_code_drift, frozen_state, golden_freeze_gate

from deploy_lib.golden_plan import golden_hash_entries
from estate import config


def generate_stage_configs_and_hashes(ctx, comp_dir):
# Write the nakon/stage configs and compute every golden hash, onto `ctx`.
#
#     The M4 golden hashes are computed BEFORE phase 1 — phase 4's rebuild gate and the
#     tfvars both consume them — and the frozen gate fires here so a config-class freeze
#     refuses before any Proxmox mutation. The golden bundle is built now (content
#     addressed; the phase-4 plant reuses the cache) because each box's hash consumes its
#     plan's payload shas. See the comments on each sub-block for the incident history.
    ctx.nakon_config_path = generate_nakon_config(ctx.teams, ctx.boxes, ctx.difficulty, comp_dir, ctx.box_password,
                                               box_username=ctx.box_username)
    # M3.2: the full bundle is never deployed as one pass anymore. The golden-stage
    # bundle is built inside golden_ops at plant time; the pre-domain bundle in phase 6 and
    # the final bundle in phase 7 (all content-addressed, so resumes hit the cache).
    # Domain controllers keep an unbooted golden so each team's forest specializes its
    # own machine SID before promotion; their configs move to the pre-domain stage.
    ctx.unbooted = unbooted_golden_boxes(comp_dir)
    ctx.golden_config_path, ctx.predomain_config_path, ctx.final_config_path, _postclone_path = generate_stage_configs(
        comp_dir, ctx.teams, ctx.boxes, unbooted=ctx.unbooted)

    # M4: template hashes are computed BEFORE phase 1 — phase 4's rebuild gate and the
    # tfvars both consume them. The golden bundle is built now (content-addressed; the
    # phase-4 plant reuses the cache) because each box's hash consumes its plan's payload
    # shas. The frozen gate also fires HERE, before any Proxmox mutation ("nothing
    # destroyed" is the whole point of the freeze; the phase-4 per-box gate alone was too
    # late, matrix run 4).
    node = config.active().proxmox.node
    golden_bundle = build_nakon_bundle(ctx.golden_config_path)
    golden_machines_by_box = {
        m["name"].rsplit("-golden", 1)[0]: m
        for m in json.loads(ctx.golden_config_path.read_text())["machines"]
    }
    base_template_ids = _template_vmid_map(node)
    ctx.golden_inputs, ctx.golden_hashes = golden_hash_entries(
        ctx.boxes, base_template_ids, golden_machines_by_box, golden_bundle,
        ctx.box_password, ctx.box_username, config.active().ssh.public_key,
        ctx.apt_cache, unbooted=ctx.unbooted)

    frozen = frozen_state(comp_dir)
    if frozen:
        # The frozen record's `code` (commit + dirty) was written but never read: the
        # per-template drift gate below keys on input hashes and its code-class verdict
        # is warn-only, so a commit after --freeze used to run silently on unverified
        # code. Warn once here, before the golden loop, naming both commits.
        code_warning = frozen_code_drift(frozen)
        if code_warning:
            print(code_warning)
        # Goldens now: config-class drift refuses here, before any Proxmox mutation.
        # The engine's gate still runs at phase 2 (its inputs are computed there), and
        # phase 4's rebuild gate applies the same verdict before it destroys a golden —
        # code-only drift warns and proceeds off the frozen template.
        frozen_hashes = (frozen.get("hashes") or {})
        # `box_name`, NOT the event name: this loop used to read `for spec.name in
        # generated.golden_hashes`, which assigned each golden box name onto the spec
        # and left the LAST box there. On any FROZEN competition that clobbered
        # the Compfile event name before its later uses, so the deploy's
        # `event_name` — and therefore `local.comp_tag`, the ownership tag on the engine
        # and every team box — became "comp-web01" instead of "comp-<competition>", so
        # every ownership-gated path (preflight, teardown) mis-classified the range;
        # confirm_deploy also showed the wrong name. Fail-safe rather than destructive,
        # but it strands infrastructure and the next deploy collides on those vmids.
        for box_name in ctx.golden_hashes:
            stored_inputs = (frozen_hashes.get("golden") or {}).get(box_name, {}).get("inputs") or {}
            golden_freeze_gate(box_name, stored_inputs, ctx.golden_inputs[box_name],
                               frozen.get("frozen_at"))
