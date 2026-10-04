"""Teardown of the API-created templates (goldens, jump VMs, engine template).

They are not in terraform state and are the linked clones' base disks, so they die only
after `terraform destroy` removed every clone — and only on a --full teardown."""

import os

from constants import GOLDEN_TAG, SCORING_ENGINE_VMID, ownership_tags
from golden_ops import destroy_golden_set
from jump_ops import destroy_jump_vms
from nodes_ops import record_of
from template_ops import destroy_engine_template


def engine_vmid_from_state(deployed_state):
    engine_vmid = deployed_state.get("scoring_vm_id") or SCORING_ENGINE_VMID
    try:
        return int(engine_vmid)
    except (TypeError, ValueError):
        return SCORING_ENGINE_VMID


def teardown_templates(comp_dir, competition, run_id, boxes, deployed_state, placement, full):
    """Destroy (--full) or keep (teams-only) the golden + engine templates."""
    engine_vmid = engine_vmid_from_state(deployed_state)
    node = os.environ.get("TF_VAR_proxmox_node", "pve")
    if full:
        print(f"  Destroying golden templates (engine vmid {engine_vmid} + 150 + i)...")
        destroy_golden_set(node, engine_vmid, len(boxes), slot=0,
                           expect_tags=ownership_tags(competition, run_id, GOLDEN_TAG))
        if placement and placement["satellites"]:
            # Multi-node: each satellite's golden copies and the jump VMs die on
            # their own hosts, then the engine template here.
            for sat in placement["satellites"]:
                sat_rec = record_of(placement, sat["name"])
                print(f"  Destroying satellite '{sat['name']}' golden set (slot "
                      f"{sat['slot']}) + jump vmid {sat['jump_vmid']}...")
                destroy_golden_set(sat_rec.node, engine_vmid, len(boxes), slot=sat["slot"],
                                   expect_tags=ownership_tags(competition, run_id, GOLDEN_TAG))
            destroy_jump_vms(placement, ownership_tags(competition, run_id))
        print(f"  Destroying the engine template (vmid {engine_vmid} + 140)...")
        destroy_engine_template(node, engine_vmid,
                                expect_tags=ownership_tags(competition, run_id,
                                                           "engine-template"))
        # A destroyed template's hash record is a loaded gun for the reuse path: the
        # next deploy would 'reuse' a hash with nothing behind it and clone from a
        # dead vmid. Drop the record alongside the templates.
        hashes_path = comp_dir / ".template-hashes.json"
        if hashes_path.exists():
            hashes_path.unlink()
            print("  Template hash record (.template-hashes.json) removed.")
    else:
        print("  Teams-only teardown — templates kept for THIS competition's next run only.")
        print("  Goldens never carry across competitions; tear down --full once the run's goal is met.")
