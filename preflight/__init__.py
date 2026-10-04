"""Pre-Proxmox safety gates. Entry point: `preflight.run_preflight(plan)` (gate.py)."""

from preflight.catalog import catalog_check_paths, catalog_gate  # noqa: F401
from preflight.concurrency import gate_concurrent_deploys  # noqa: F401
from preflight.gate import preflight_gates, preflight_gates_multinode, run_preflight  # noqa: F401
from preflight.headroom import check_datastore_headroom  # noqa: F401
from preflight.mgmt_ip import engine_mgmt_ip_gate  # noqa: F401
from preflight.plan import NodeShare, PreflightPlan, multinode_plan, single_node_plan  # noqa: F401
from preflight.templates import cloudinit_gate, template_cloudinit_missing  # noqa: F401

__all__ = [
    "NodeShare",
    "PreflightPlan",
    "catalog_check_paths",
    "catalog_gate",
    "check_datastore_headroom",
    "cloudinit_gate",
    "engine_mgmt_ip_gate",
    "gate_concurrent_deploys",
    "multinode_plan",
    "preflight_gates",
    "preflight_gates_multinode",
    "run_preflight",
    "single_node_plan",
    "template_cloudinit_missing",
]
