"""Back-compat facade for the M4 per-competition template lifecycle (build -> reuse -> freeze ->
destroy). Split by responsibility; every name stays importable from `template_ops`:

  template_freeze.py      hashes, hash inputs, frozen state/drift gates, git code-drift probe
  engine_template_ops.py  find / build / destroy the engine template VM

Patching a name HERE does not change the moved implementation - patch the owning module
(e.g. `patch.object(engine_template_ops, "destroy_vm_if_exists")`)."""

from engine_template_ops import (build_engine_template, destroy_engine_template,  # noqa: F401
                                 find_engine_template)
from template_freeze import (FROZEN_FILE, HASHES_FILE, bundle_id, canonical_json,  # noqa: F401
                             classify_drift, code_hash, code_path_dirty, engine_hash_inputs,
                             engine_template_vmid, frozen_code_drift, frozen_gate, frozen_state,
                             git_commit_info, golden_freeze_gate, golden_hash_inputs,
                             golden_payload_hash, golden_plant_checkpoints, hash_from_inputs,
                             load_template_hashes, save_template_hashes, sha256_text,
                             stored_template_hash, tf_resource_block, write_template_hash)

__all__ = [
    "FROZEN_FILE",
    "HASHES_FILE",
    "build_engine_template",
    "bundle_id",
    "canonical_json",
    "classify_drift",
    "code_hash",
    "code_path_dirty",
    "destroy_engine_template",
    "engine_hash_inputs",
    "engine_template_vmid",
    "find_engine_template",
    "frozen_code_drift",
    "frozen_gate",
    "frozen_state",
    "git_commit_info",
    "golden_freeze_gate",
    "golden_hash_inputs",
    "golden_payload_hash",
    "golden_plant_checkpoints",
    "hash_from_inputs",
    "load_template_hashes",
    "save_template_hashes",
    "sha256_text",
    "stored_template_hash",
    "tf_resource_block",
    "write_template_hash",
]
