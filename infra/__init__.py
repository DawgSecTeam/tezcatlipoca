"""infra: everything that touches the Proxmox estate lives here.

  client      the one Proxmox API client (TLS pinning, per-node routes, cluster reads,
              task wait, node-load throttle). Everything else talks to Proxmox through it.
  guest_exec  guest-agent exec / detached exec / agent waits
  lifecycle   start/stop + snapshots
  ownership   ownership-guarded mutations (destroy / retag / unlock / gc / clone marker)
  console     VNC console sessions (vncproxy + vncwebsocket)
  gates       every refusal the deploy makes before it builds or destroys anything
  plan        preflight plan model (PreflightPlan / NodeShare + builders)
  collisions  live-cluster overlap refusals (vmid/bridge slot, mgmt IP)
  resources   node resource gates (datastore headroom, template/cloud-init checks)

Import from the owning submodule (`from infra.client import proxmox_api`); the names
re-exported here are the public surface for callers that want the package-level import.
Patching: patch the owning module, not this package."""

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
