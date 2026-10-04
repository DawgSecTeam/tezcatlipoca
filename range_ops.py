"""Back-compat facade: the Proxmox/range primitives that used to live here are now split by
responsibility. Import from the owning module in new code; every name below stays importable
from `range_ops` with an unchanged signature.

  pve_api.py            API client, node routes, cluster reads, task wait, node-load throttle
  guest_exec.py         guest-agent exec / detached exec / file pull / agent waits
  vm_ownership.py       ownership-guarded mutations (destroy / retag / unlock / gc / clone marker)
  vm_lifecycle.py       start/stop + snapshots
  targets.py            (team, box) target abstraction + vmid arithmetic
  terraform_workdir.py  per-competition terraform dir + state vmids

NOTE: this is NOT a constants shim (constants come from constants.py directly), and patching a
name HERE does not affect the moved implementation - tests patch the owning module
(e.g. `patch.object(pve_api, "proxmox_api")`, `patch.object(guest_exec.time, ...)`)."""

from guest_exec import (DETACHED_RC_MARKER, DetachedExecResult, diagnose_unreachable_box,  # noqa: F401
                        guest_agent_exec_detached, guest_agent_exec_root,
                        guest_agent_exec_windows, guest_file_read, wait_for_guest_agent)
from pve_api import (_NODE_ROUTES, clear_node_routes, cluster_vms_for, live_vmids,  # noqa: F401
                     node_loadavg, node_routes_active, proxmox_api, proxmox_api_for,
                     proxmox_request, register_node_routes, wait_for_node_load,
                     wait_for_proxmox_task)
from targets import (box_index, describe_target, enumerate_targets, load_targets,  # noqa: F401
                     persist_targets, vm_id_for)
from terraform_workdir import (REPO_ROOT, ensure_terraform_workdir, team_vmids_from_state,  # noqa: F401
                               terraform_dir, terraform_plugin_cache_dir)
from vm_lifecycle import (delete_snapshot, list_snapshots, rollback_snapshot,  # noqa: F401
                          snapshot_support_hint, start_vm, stop_vm, take_snapshot, vm_status)
from vm_ownership import (clone_marker, destroy_vm_if_exists, gc_orphan_volumes,  # noqa: F401
                          has_clone_marker, parse_vm_tags, retag_ownership, unlock_vm)

__all__ = [
    "DETACHED_RC_MARKER",
    "DetachedExecResult",
    "REPO_ROOT",
    "_NODE_ROUTES",
    "box_index",
    "clear_node_routes",
    "clone_marker",
    "cluster_vms_for",
    "delete_snapshot",
    "describe_target",
    "destroy_vm_if_exists",
    "diagnose_unreachable_box",
    "ensure_terraform_workdir",
    "enumerate_targets",
    "gc_orphan_volumes",
    "guest_agent_exec_detached",
    "guest_agent_exec_root",
    "guest_agent_exec_windows",
    "guest_file_read",
    "has_clone_marker",
    "list_snapshots",
    "live_vmids",
    "load_targets",
    "node_loadavg",
    "node_routes_active",
    "parse_vm_tags",
    "persist_targets",
    "proxmox_api",
    "proxmox_api_for",
    "proxmox_request",
    "register_node_routes",
    "retag_ownership",
    "rollback_snapshot",
    "snapshot_support_hint",
    "start_vm",
    "stop_vm",
    "take_snapshot",
    "team_vmids_from_state",
    "terraform_dir",
    "terraform_plugin_cache_dir",
    "unlock_vm",
    "vm_id_for",
    "vm_status",
    "wait_for_guest_agent",
    "wait_for_node_load",
    "wait_for_proxmox_task",
]
