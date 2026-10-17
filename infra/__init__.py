"""infra: everything that touches the Proxmox estate lives here."""
# Patch the owning submodule, never this package: the names here are re-exports.

from infra.client import (clear_node_routes, cluster_vms_for, live_vmids,  # noqa: F401
                          node_routes_active, proxmox_api,
                          proxmox_api_for, proxmox_request, register_node_routes,
                          wait_for_proxmox_task)
from infra.console import (mint_console_tokens, node_targets,  # noqa: F401
                           probe_permissions, revoke_console_tokens)
from infra.guest_exec import (DETACHED_RC_MARKER, DetachedExecResult,  # noqa: F401
                              diagnose_unreachable_box, guest_agent_exec_detached,
                              guest_agent_exec_root, guest_agent_exec_windows,
                              wait_for_guest_agent)
from infra.lifecycle import (delete_snapshot, list_snapshots, rollback_snapshot,  # noqa: F401
                             snapshot_support_hint, start_vm, stop_vm, take_snapshot,
                             vm_status)
from infra.ownership import (Ownership, Refusal, clone_marker, destroy_vm_if_exists,  # noqa: F401
                             gc_orphan_volumes, has_clone_marker, parse_vm_tags,
                             retag_ownership, unlock_vm)
from infra.gates import (acquire_deploy_lock, catalog_gate, check_resume_gates,  # noqa: F401
                         run_range_gates)
