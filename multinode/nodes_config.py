"""nodes.json: multi-node constants, NodeRecord, config loading and per-node env application."""

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, fields, asdict
from pathlib import Path
from urllib.parse import urlparse

from common.constants import JUMP_VMID_OFFSET_LOW
from estate import config

NODES_CONFIG_PATH = Path("nodes.json")

MAX_SATELLITES = 4
# Jump/router VM per satellite: engine_vmid + 130 + slot (slot 0 is the engine, no jump),
# below the engine-template slot (+140) so one preflight scan covers the reserved span.
JUMP_VMID_OFFSET = JUMP_VMID_OFFSET_LOW
# Jump mgmt IPs walk DOWN from .239: red01 owns .240-.249 and the engine's default is .250,
# so starting at .249 put a jump and red01 on the same address (they share the mgmt L2).
JUMP_MGMT_IP_BASE = "10.0.0.239"

PLACEMENT_VERSION = 2
JUMP_RESERVE_MB = 512


@dataclass
class NodeRecord:
    name: str
    endpoint: str
    node: str
    datastore: str
    token_env: str
    engine_base_vmid: int = 0
    engine_mgmt_ip: str = ""
    engine_mgmt_gw: str = ""
    jump_mgmt_ip: str = ""
    mgmt_bridge: str = ""
    ssh_host: str = ""
    jump_template: str = ""
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
            mgmt_bridge=str(raw.get("mgmt_bridge") or ""),
            ssh_host=str(raw.get("ssh_host") or ""),
            jump_template=str(raw.get("jump_template") or ""),
            # `or` would turn an explicit 0 into the default: 0 teams is how an
            # engine-only node is declared.
            weight=float(raw["weight"]) if raw.get("weight") is not None else 1.0,
            max_teams=int(raw["max_teams"]) if raw.get("max_teams") is not None else 8,
            tls_fingerprint=str(raw.get("tls_fingerprint") or ""),
        )
        _require(rec.weight > 0, f"node '{rec.name}': weight must be > 0")
        _require(rec.max_teams >= 0, f"node '{rec.name}': max_teams must be >= 0")
        _require(rec.endpoint.startswith(("http://", "https://")),
                 f"node '{rec.name}': endpoint must be an http(s) URL")
        records.append(rec)
    names = [r.name for r in records]
    _require(len(set(names)) == len(names), f"duplicate node names: {names}")
    _require(len(set(r.node for r in records)) == len(records),
             "duplicate 'node' (PVE host) names — the route table keys on them")
    dupes = sorted({r.token_env for r in records if
                    sum(1 for o in records if o.token_env == r.token_env) > 1})
    if dupes:
        holders = {d: [r.name for r in records if r.token_env == d] for d in dupes}
        raise SystemExit(
            f"  ERROR: nodes.json gives {len(dupes)} token_env name(s) to more than one "
            f"node: " + "; ".join(f"{d} -> {', '.join(n)}" for d, n in holders.items())
            + ". Each node needs its OWN env var (e.g. TEZ_PROXMOX_API_TOKEN_150 / "
              "_193), so every host's route carries its own token. "
              "See docs/multi-node.md.")
    return records, dict(data.get("balancing") or {})



def apply_node_env(record):
# In-process config override, not an os.environ mutation: env writes leaked into every
#     later read.
    token = os.environ.get(record.token_env)
    if not token:
        raise SystemExit(
            f"  ERROR: nodes.json node '{record.name}' points at token_env "
            f"'{record.token_env}' but that env var is empty/absent — set the token "
            f"in the environment (never in nodes.json, which is committed).")
    values = {
        "proxmox.endpoint": record.endpoint,
        "proxmox.api_token": token,
        "proxmox.node": record.node,
        "proxmox.datastore": record.datastore,
        "proxmox.template_vm_id": record.engine_base_vmid,
        # Set unconditionally: a node with no pin of its own must not inherit the previous
        # node's fingerprint (they are different hosts).
        "proxmox.tls_fingerprint": record.tls_fingerprint or "",
    }
    if record.mgmt_bridge:
        values["proxmox.mgmt_bridge"] = record.mgmt_bridge
    return config.active().activate(values)


def restore_node_env(restore):
    config.active().deactivate(restore)


@contextmanager
def node_env(record):
    restore = apply_node_env(record)
    try:
        yield record
    finally:
        restore_node_env(restore)
