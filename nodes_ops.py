"""Multi-node placement facade. The model (docs/multi-node.md): one competition spans up to
MAX_SATELLITES+1 Proxmox hosts. The ENGINE node (slot 0) hosts the scoring engine, the engine
template and its own teams; each SATELLITE node hosts some teams, their goldens and a per-
competition alpine jump VM (jump_ops). placement.json is the authoritative per-competition record.
No nodes.json and no placement.json => every function is a no-op (single-node pipeline).

Split by responsibility; every name stays importable from `nodes_ops`:

  nodes_config.py       nodes.json, NodeRecord, constants, per-node env application
  placement_record.py   placement.json I/O, accessors, activate/deactivate, vmid slot math,
                        satellite tfvars/routes
  placement_planner.py  node probes, capacity-fill compute, decision table, resolve_placement

Patching a name HERE does not change the moved implementation - patch the owning module."""

from nodes_config import (JUMP_MGMT_IP_BASE, JUMP_RESERVE_MB, JUMP_VMID_OFFSET,  # noqa: F401
                          MAX_SATELLITES, NODES_CONFIG_PATH, NodeRecord, PLACEMENT_VERSION,
                          apply_node_env, load_nodes_config, node_env, restore_node_env)
from placement_planner import (compute_placement, placement_decision_table,  # noqa: F401
                               probe_node, resolve_placement)
from placement_record import (activate_placement, anchor_identifier,  # noqa: F401
                              deactivate_placement, default_jump_mgmt_ip, engine_record,
                              golden_slot_span, golden_vmid_for_slot, jump_vmid_for,
                              node_of_team, read_placement, record_of, satellite_routes_for,
                              satellite_tfvars, slot_of_team, teams_on_node, write_placement)

__all__ = [
    "JUMP_MGMT_IP_BASE", "JUMP_RESERVE_MB", "JUMP_VMID_OFFSET", "MAX_SATELLITES",
    "NODES_CONFIG_PATH", "NodeRecord", "PLACEMENT_VERSION", "activate_placement",
    "anchor_identifier", "apply_node_env", "compute_placement", "deactivate_placement",
    "default_jump_mgmt_ip", "engine_record", "golden_slot_span", "golden_vmid_for_slot",
    "jump_vmid_for", "load_nodes_config", "node_env", "node_of_team",
    "placement_decision_table", "probe_node", "read_placement", "record_of", "resolve_placement",
    "restore_node_env", "satellite_routes_for", "satellite_tfvars", "slot_of_team",
    "teams_on_node", "write_placement",
]
