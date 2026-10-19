"""Generated nakon/stage configs and the golden hashes + frozen gate computed from them."""

import json

from golden.golden_target_ops import _template_vmid_map, unbooted_golden_boxes
from nakonops.nakon_bundle_ops import build_nakon_bundle
from nakonops.nakon_config_ops import generate_nakon_config, generate_stage_configs
from engine.template_freeze import frozen_code_drift, frozen_state, golden_freeze_gate

from deploy_lib.golden_plan import golden_hash_entries
from estate import config


def generate_stage_configs_and_hashes(ctx, comp_dir):
# Golden hashes and the config-class frozen gate are computed BEFORE phase 1 — phase 4's
    # rebuild gate and the estate apply consume them — and the frozen gate fires here so a
    # freeze refuses before any Proxmox mutation.
    ctx.nakon_config_path = generate_nakon_config(ctx.teams, ctx.boxes, ctx.difficulty, comp_dir, ctx.box_password,
                                               box_username=ctx.box_username)
    # The full bundle is never deployed as one pass: the golden-stage bundle is built inside
    # golden_ops at plant time, the pre-domain one in phase 6 and the final one in phase 7
    # (all content-addressed, so resumes hit the cache). Domain controllers keep an unbooted
    # golden so each team's forest specializes its own machine SID before promotion.
    ctx.unbooted = unbooted_golden_boxes(comp_dir)
    (ctx.golden_config_path, ctx.predomain_config_path, ctx.final_config_path,
     _postclone_path, ctx.late_config_path) = generate_stage_configs(
        comp_dir, ctx.teams, ctx.boxes, unbooted=ctx.unbooted)

    # The golden bundle is built now (content addressed; the phase-4 plant reuses the cache)
    # because each box's hash consumes its plan's payload shas.
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
        # per-template drift gate keys on input hashes and its code verdict is warn-only, so a
        # commit after --freeze would run silently on unverified code. Warn once, here.
        code_warning = frozen_code_drift(frozen)
        if code_warning:
            print(code_warning)
        # Config-class drift refuses here, before any Proxmox mutation; phase 4's rebuild gate
        # applies the same verdict before it destroys a golden, and code-only drift warns.
        frozen_hashes = (frozen.get("hashes") or {})
        # `box_name`, NOT the event name: this loop used to bind each golden box name onto the
        # spec and leave the LAST box there, which clobbered the Compfile event name — and so
        # `local.comp_tag`, the ownership tag on the engine and every team box.
        for box_name in ctx.golden_hashes:
            stored_inputs = (frozen_hashes.get("golden") or {}).get(box_name, {}).get("inputs") or {}
            golden_freeze_gate(box_name, stored_inputs, ctx.golden_inputs[box_name],
                               frozen.get("frozen_at"))
