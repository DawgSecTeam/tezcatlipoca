"""nodes.json: multi-node constants, NodeRecord, config loading and per-node env application.

The module docstring of the multi-node model lives in docs/multi-node.md; nodes_ops re-exports
everything here and in placement_record / placement_planner."""

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, fields, asdict
from pathlib import Path
from urllib.parse import urlparse

NODES_CONFIG_PATH = Path("nodes.json")

MAX_SATELLITES = 4
# Jump/router VM per satellite: engine_vmid + 130 + slot (slots 1..4 — slot 0 is the
# engine node and has no jump). Below the engine-template slot (+140) so one preflight
# scan still covers the whole reserved span.
JUMP_VMID_OFFSET = 130
# Default jump mgmt IPs walk down from .249 (slot 1) — clear of the engine's default
# .250 and of the red01 slots (.198/.199/.244). Collision-swept in preflight; override
# per node with jump_mgmt_ip in nodes.json.
JUMP_MGMT_IP_BASE = "10.0.0.249"

PLACEMENT_VERSION = 1
JUMP_RESERVE_MB = 512  # capacity reservation per satellite (tiny router VM)

_MISSING = object()

_ENV_KEYS = ("TF_VAR_proxmox_endpoint", "TF_VAR_proxmox_api_token", "TF_VAR_proxmox_node",
             "TF_VAR_datastore", "TF_VAR_template_vm_id")


@dataclass
class NodeRecord:
    name: str
    endpoint: str
    node: str
    datastore: str
    token_env: str
    engine_base_vmid: int = 0
    engine_mgmt_ip: str = ""   # static engine mgmt IP when this node hosts the engine
    engine_mgmt_gw: str = ""
    jump_mgmt_ip: str = ""     # static jump mgmt IP when this node is a satellite
    ssh_host: str = ""         # for root SSH (template sync); defaults to endpoint host
    jump_template: str = ""    # name substring for the jump clone source (default "alpine")
    weight: float = 1.0
    max_teams: int = 8
    tls_fingerprint: str = ""

    def host(self):
        return self.ssh_host or urlparse(self.endpoint).hostname or self.name

    def to_json(self):
        return asdict(self)

    @classmethod
    def from_json(cls, data):
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def _require(cond, msg):
    if not cond:
        raise SystemExit(f"  ERROR: nodes.json: {msg}")


def load_nodes_config(path=None):
    """Parse and validate nodes.json. Returns (records, balancing) or (None, None)
    when the file is absent — the single-node signal."""
    path = Path(path or NODES_CONFIG_PATH)
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"  ERROR: {path} is unreadable/malformed ({str(e)[:120]})")
    _require(isinstance(data, dict), "top level must be an object")
    raw_nodes = data.get("nodes")
    _require(isinstance(raw_nodes, list) and raw_nodes, "'nodes' must be a non-empty list")
    records = []
    for i, raw in enumerate(raw_nodes):
        _require(isinstance(raw, dict), f"nodes[{i}] must be an object")
        for key in ("name", "endpoint", "node", "datastore", "token_env"):
            _require(raw.get(key), f"nodes[{i}] is missing required field '{key}'")
        rec = NodeRecord(
            name=str(raw["name"]),
            endpoint=str(raw["endpoint"]).rstrip("/"),
            node=str(raw["node"]),
            datastore=str(raw["datastore"]),
            token_env=str(raw["token_env"]),
            engine_base_vmid=int(raw.get("engine_base_vmid") or 0),
            engine_mgmt_ip=str(raw.get("engine_mgmt_ip") or ""),
            engine_mgmt_gw=str(raw.get("engine_mgmt_gw") or ""),
            jump_mgmt_ip=str(raw.get("jump_mgmt_ip") or ""),
            ssh_host=str(raw.get("ssh_host") or ""),
            jump_template=str(raw.get("jump_template") or ""),
            weight=float(raw.get("weight") or 1.0),
            max_teams=int(raw.get("max_teams") or 8),
            tls_fingerprint=str(raw.get("tls_fingerprint") or ""),
        )
        _require(rec.weight > 0, f"node '{rec.name}': weight must be > 0")
        _require(rec.endpoint.startswith(("http://", "https://")),
                 f"node '{rec.name}': endpoint must be an http(s) URL")
        records.append(rec)
    names = [r.name for r in records]
    _require(len(set(names)) == len(names), f"duplicate node names: {names}")
    _require(len(set(r.node for r in records)) == len(records),
             "duplicate 'node' (PVE host) names — the route table keys on them")
    # token_env must be unique per record. activate_placement installs each node's token
    # into the env var NAMED by its record before snapshotting the route table, so two
    # records sharing one name means whichever is applied last wins for both: the scale8
    # soak's satellite 401'd mid-preflight against a token belonging to the other node
    # (2026-10-02). The workaround was hand-unique names; this rejects the collision
    # instead of letting it surface as an authentication failure on the far node.
    dupes = sorted({r.token_env for r in records if
                    sum(1 for o in records if o.token_env == r.token_env) > 1})
    if dupes:
        holders = {d: [r.name for r in records if r.token_env == d] for d in dupes}
        raise SystemExit(
            f"  ERROR: nodes.json gives {len(dupes)} token_env name(s) to more than one "
            f"node: " + "; ".join(f"{d} -> {', '.join(n)}" for d, n in holders.items())
            + ". Each node needs its OWN env var (e.g. TF_VAR_proxmox_api_token_150 / "
              "_193), because the placement applies one record's token at a time into the "
              "shared name and the last write would otherwise be used for every node. "
              "See docs/multi-node.md.")
    return records, dict(data.get("balancing") or {})


# ---------------------------------------------------------------- env application

def apply_node_env(record):
    """Point the process env (TF_VAR_*) at `record` — the engine-node record for a
    deploy, or the recorded engine node for companion ops. Terraform reads the same
    env natively, and every env-sourced read in the pipeline keeps working. Returns
    a restore dict for restore_node_env()."""
    token = os.environ.get(record.token_env)
    if not token:
        raise SystemExit(
            f"  ERROR: nodes.json node '{record.name}' points at token_env "
            f"'{record.token_env}' but that env var is empty/absent — add the token "
            f"to .env (never to nodes.json, which is committed).")
    restore = {k: os.environ.get(k, _MISSING) for k in _ENV_KEYS}
    os.environ["TF_VAR_proxmox_endpoint"] = record.endpoint
    os.environ["TF_VAR_proxmox_api_token"] = token
    os.environ["TF_VAR_proxmox_node"] = record.node
    os.environ["TF_VAR_datastore"] = record.datastore
    os.environ["TF_VAR_template_vm_id"] = str(record.engine_base_vmid)
    if record.tls_fingerprint:
        restore.setdefault("PROXMOX_TLS_FINGERPRINT",
                           os.environ.get("PROXMOX_TLS_FINGERPRINT", _MISSING))
        os.environ["PROXMOX_TLS_FINGERPRINT"] = record.tls_fingerprint
    return restore


def restore_node_env(restore):
    for k, v in restore.items():
        if v is _MISSING:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@contextmanager
def node_env(record):
    restore = apply_node_env(record)
    try:
        yield record
    finally:
        restore_node_env(restore)
