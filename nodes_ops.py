"""Multi-node placement: nodes.json config, capacity-fill team placement, and the
per-competition placement record (placement.json).

Model: one competition spans up to MAX_SATELLITES+1 Proxmox hosts. The ENGINE node
(slot 0) hosts the scoring engine, the engine template, and its own local teams;
each SATELLITE node (slots 1..) hosts some teams, their golden templates, and a
per-competition alpine jump/router VM that impersonates the engine's gateway IP on
the satellite's team bridges (see jump_ops). Routing over the shared mgmt LAN links
the pieces; from every box's perspective nothing changes (docs/multi-node.md).

Nodes are independent API endpoints — each NodeRecord carries its own endpoint,
token (by env-var NAME — this file is committed, tokens live in .env), node name,
datastore, and engine base vmid. placement.json is the authoritative record per
competition: deploy writes it once and every later op (resume/redeploy/verify/
destroy) reads it back, so a changed .env or a re-run balancer can never move
someone else's range onto the wrong host.

No nodes.json anywhere and no placement.json => every function here is a no-op and
the pipeline behaves exactly as the single-node design it was."""

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields, asdict
from pathlib import Path
from urllib.parse import urlparse

from constants import GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM

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
    when the file is absent — the legacy single-node signal."""
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


# ---------------------------------------------------------------- placement record

def read_placement(comp_dir):
    path = Path(comp_dir) / "placement.json"
    if not path.exists():
        return None
    try:
        placement = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"  ERROR: {path} is unreadable/malformed ({str(e)[:120]}) — "
                         "this competition was placed multi-node; the placement record "
                         "is authoritative for every op against it")
    if placement.get("version") != PLACEMENT_VERSION:
        raise SystemExit(f"  ERROR: {path} was written by placement schema v"
                         f"{placement.get('version')}; this code understands "
                         f"v{PLACEMENT_VERSION}. Tear the competition down and redeploy.")
    return placement


def write_placement(comp_dir, placement):
    path = Path(comp_dir) / "placement.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(placement, indent=2))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)  # token references only, never token values
    except OSError:
        pass
    return path


def record_of(placement, node_name):
    return NodeRecord.from_json(placement["nodes"][node_name])


def engine_record(placement):
    return record_of(placement, placement["engine_node"])


def node_of_team(placement, team_key):
    return placement["team_nodes"][team_key]


def slot_of_team(placement, team_key):
    return placement["slots"][node_of_team(placement, team_key)]


def teams_on_node(placement, node_name):
    return sorted(k for k, n in placement["team_nodes"].items() if n == node_name)


def anchor_identifier(placement, node_name):
    """The team subnet this node's golden boxes sit on: the first (lowest-identifier)
    team placed there. Slot 0 anchors on team1 — today's golden math exactly."""
    teams = teams_on_node(placement, node_name)
    if not teams:
        return None
    ids = {k: placement["team_identifiers"][k] for k in teams}
    return ids[min(ids, key=lambda k: int(ids[k]))]


def activate_placement(placement):
    """Make the whole process multi-node-aware: apply the engine node's env (terraform
    + every env-sourced read) and register the node->endpoint route table that routes
    node-scoped proxmox_api calls to the owning host. Returns the env restore dict."""
    from range_ops import register_node_routes
    engine = engine_record(placement)
    restore = apply_node_env(engine)
    # Keyed by PVE host name ("proxmox"/"pve") — every node-scoped API path uses
    # /nodes/<pve-name>/..., not the nodes.json record name.
    routes = {rec["node"]: (rec["endpoint"], os.environ[rec["token_env"]])
              for rec in placement["nodes"].values()}
    register_node_routes(routes)
    return restore


def deactivate_placement(restore):
    from range_ops import clear_node_routes
    clear_node_routes()
    restore_node_env(restore)


# ---------------------------------------------------------------- vmid slot math

def golden_vmid_for_slot(engine_vmid, slot, box_idx):
    """Golden blocks per node slot: slot 0 keeps the historical engine_vmid+150+i;
    each satellite shifts by one box-stride so the same box type can exist as a
    golden on several hosts (linked clones can't cross nodes on separate storages).
    vmids only need uniqueness per PVE instance — the stride exists so this also
    holds on a future single-cluster backend."""
    return int(engine_vmid) + GOLDEN_VMID_OFFSET + slot * MAX_BOXES_PER_TEAM + box_idx


def jump_vmid_for(engine_vmid, slot):
    return int(engine_vmid) + JUMP_VMID_OFFSET + slot


def default_jump_mgmt_ip(slot):
    """10.0.0.249 for slot 1, .248 for slot 2, ..."""
    octet = int(JUMP_MGMT_IP_BASE.split(".")[-1]) - (slot - 1)
    if octet < 1:
        raise SystemExit(f"  ERROR: jump mgmt IP range exhausted at slot {slot} — "
                         "set explicit jump_mgmt_ip values in nodes.json")
    return ".".join(JUMP_MGMT_IP_BASE.split(".")[:3]) + f".{octet}"


def golden_slot_span(engine_vmid, slot):
    return [golden_vmid_for_slot(engine_vmid, slot, i) for i in range(MAX_BOXES_PER_TEAM)]


# ---------------------------------------------------------------- node probe

def probe_node(record, need_templates, engine_vmid, team_identifiers):
    """Read-only eligibility probe of one node. Returns a dict; ok=False carries the
    reason. need_templates: template names this node might have to host (per-team
    eligibility checks them; a node missing some is still usable for other teams)."""
    from range_ops import proxmox_api_for
    probe = {"name": record.name, "node": record.node, "ok": True, "reasons": [],
             "missing_templates": [], "collisions": [], "mem_free_bytes": None,
             "datastore_free": None, "running_ours": 0, "templates": {}}
    ep, token = record.endpoint, os.environ.get(record.token_env, "")
    if not token:
        return {**probe, "ok": False,
                "reasons": [f"token env '{record.token_env}' empty"]}
    try:
        status = proxmox_api_for(ep, token, "GET", f"/nodes/{record.node}/status")["data"]
        vms = proxmox_api_for(ep, token, "GET", "/cluster/resources",
                              params={"type": "vm"})["data"]
        probe["mem_free_bytes"] = (status.get("memory") or {}).get("free")
        try:
            st = proxmox_api_for(ep, token, "GET",
                                 f"/nodes/{record.node}/storage/{record.datastore}/status")["data"]
            probe["datastore_free"] = st.get("avail")
        except Exception:
            probe["datastore_free"] = None
    except Exception as e:
        return {**probe, "ok": False, "reasons": [f"API unreachable: {str(e)[:120]}"]}

    for vm in vms:
        if vm.get("node") != record.node:
            continue
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if vm.get("template") == 1 and "template" in tags and vm.get("status") == "stopped":
            probe["templates"][vm.get("name")] = vm["vmid"]
        if "tezcatlipoca" in tags and vm.get("status") == "running":
            probe["running_ours"] += 1
    probe["missing_templates"] = sorted(set(need_templates) - set(probe["templates"]))

    # Quick collision veto (the authoritative per-node check is preflight): engine +
    # engine template + every slot's golden span + every candidate team block + jumps.
    existing = {vm.get("vmid") for vm in vms if vm.get("node") == record.node}
    wanted = {engine_vmid, engine_vmid + 140}
    for slot in range(MAX_SATELLITES + 1):
        wanted.update(golden_slot_span(engine_vmid, slot))
        wanted.add(jump_vmid_for(engine_vmid, slot))
    for ident in team_identifiers:
        wanted.update(range(200 + int(ident) * 10, 200 + int(ident) * 10 + MAX_BOXES_PER_TEAM))
    probe["collisions"] = sorted(wanted & existing)
    if probe["collisions"]:
        probe["ok"] = False
        probe["reasons"].append(f"vmid collisions: {probe['collisions'][:6]}"
                                + ("..." if len(probe["collisions"]) > 6 else ""))
    try:
        nets = proxmox_api_for(ep, token, "GET", f"/nodes/{record.node}/network")["data"]
        bridges = {n.get("iface") for n in nets}
        bad_bridges = [f"vmbr{i}" for i in team_identifiers if f"vmbr{i}" in bridges]
        if bad_bridges:
            probe["ok"] = False
            probe["reasons"].append(f"bridge collisions: {bad_bridges}")
    except Exception:
        pass
    return probe


# ---------------------------------------------------------------- placement compute

def _team_reservation_mb(boxes):
    return sum(int(b.get("memory_mb") or 1024) for b in boxes)


def _prefer_index(name, prefer):
    return prefer.index(name) if name in prefer else len(prefer)


def compute_placement(records, balancing, teams, boxes, engine_vmid, comp_name,
                      team_overrides=None, engine_override=None, probes=None):
    """Capacity-fill team placement. Teams pack biggest-first onto the eligible node
    with the most effective free RAM (mem_free / weight minus reservations). Explicit
    overrides win outright. Returns the placement dict (not yet persisted)."""
    team_overrides = dict(team_overrides or {})
    identifiers = {k: str(t["identifier"]) for k, t in teams.items()}
    by_identifier = {v: k for k, v in identifiers.items()}
    known = {r.name for r in records}
    resolved_overrides = {}
    for ident, node_name in team_overrides.items():
        key = by_identifier.get(ident, ident)
        if key not in teams:
            raise SystemExit(f"  ERROR: --team-node {ident}=... : no team with that "
                             f"key/identifier (teams: {', '.join(sorted(teams))})")
        if node_name not in known:
            raise SystemExit(f"  ERROR: --team-node {ident}={node_name}: no such node "
                             f"(nodes.json has: {', '.join(sorted(known))})")
        resolved_overrides[key] = node_name
    team_overrides = resolved_overrides

    need_templates = sorted({b["template"] for b in boxes})
    team_ids = sorted(set(identifiers.values()), key=int)
    probes = probes or {r.name: probe_node(r, need_templates, engine_vmid, team_ids)
                        for r in records}

    team_nodes = dict(team_overrides)
    reserved_mb = {}
    for key, node_name in team_nodes.items():
        reserved_mb[node_name] = reserved_mb.get(node_name, 0) + _team_reservation_mb(boxes)

    prefer = [str(x) for x in (balancing.get("prefer") or [])]

    def eligible(rec):
        probe = probes[rec.name]
        if not probe["ok"]:
            return False
        if not {b["template"] for b in boxes} <= set(probe["templates"]):
            return False
        placed = sum(1 for n in team_nodes.values() if n == rec.name)
        return placed < rec.max_teams

    def eff_free(rec):
        mem = probes[rec.name].get("mem_free_bytes") or 0
        return (mem / rec.weight) - reserved_mb.get(rec.name, 0) * 1024 ** 2

    skipped = []
    for key in sorted((k for k in teams if k not in team_nodes),
                      key=lambda k: -_team_reservation_mb(boxes)):
        cands = [r for r in records if eligible(r)]
        if not cands:
            skipped.append(key)
            continue
        cands.sort(key=lambda r: (-eff_free(r), _prefer_index(r.name, prefer), r.name))
        team_nodes[key] = cands[0].name
        reserved_mb[cands[0].name] = (reserved_mb.get(cands[0].name, 0)
                                      + _team_reservation_mb(boxes))
    if skipped:
        detail = "; ".join(
            f"{r.name}: "
            + ", ".join(probes[r.name]["reasons"] or probes[r.name]["missing_templates"]
                        or ["under capacity"])
            for r in records)
        raise SystemExit(
            "  ERROR: no eligible node for team(s) " + ", ".join(sorted(skipped))
            + f". Node state: {detail}. Sync missing templates (sync-template.py), free "
              "capacity, or raise max_teams/weight in nodes.json.")

    # Engine node: override, else the node holding the most teams (tie: prefer order,
    # then more free RAM).
    if engine_override:
        if engine_override not in known:
            raise SystemExit(f"  ERROR: --engine-node {engine_override}: no such node")
        engine_node = engine_override
    else:
        counts = {r.name: sum(1 for n in team_nodes.values() if n == r.name)
                  for r in records}
        best_count = max(counts.values())
        cands = [r for r in records if counts[r.name] == best_count]
        cands.sort(key=lambda r: (_prefer_index(r.name, prefer),
                                  -(probes[r.name].get("mem_free_bytes") or 0), r.name))
        engine_node = cands[0].name
    engine_rec = next(r for r in records if r.name == engine_node)
    if not engine_rec.engine_base_vmid:
        raise SystemExit(f"  ERROR: engine node '{engine_node}' has no engine_base_vmid "
                         "in nodes.json")

    used = [engine_node] + sorted(
        (n for n in set(team_nodes.values()) if n != engine_node),
        key=lambda n: (_prefer_index(n, prefer), n))
    if len(used) - 1 > MAX_SATELLITES:
        raise SystemExit(f"  ERROR: placement needs {len(used) - 1} satellites but "
                         f"MAX_SATELLITES={MAX_SATELLITES} (terraform providers are static)")

    slots = {name: i for i, name in enumerate(used)}
    satellites = []
    seen_jump_ips = {}
    pseudo = {"team_nodes": team_nodes, "team_identifiers": identifiers}
    for name in used[1:]:
        slot = slots[name]
        rec = next(r for r in records if r.name == name)
        ip = rec.jump_mgmt_ip or default_jump_mgmt_ip(slot)
        if ip in seen_jump_ips:
            raise SystemExit(f"  ERROR: duplicate jump mgmt IP {ip} for nodes "
                             f"'{seen_jump_ips[ip]}' and '{name}' — set explicit "
                             "jump_mgmt_ip values in nodes.json")
        seen_jump_ips[ip] = name
        satellites.append({
            "name": name, "slot": slot,
            "teams": [k for k, n in team_nodes.items() if n == name],
            "jump_vmid": jump_vmid_for(engine_vmid, slot),
            "jump_mgmt_ip": ip,
            "anchor_identifier": anchor_identifier(pseudo, name),
        })

    placement = {
        "version": PLACEMENT_VERSION,
        "comp": comp_name,
        "engine_vmid": engine_vmid,
        "engine_node": engine_node,
        "nodes": {r.name: r.to_json() for r in records},
        "slots": slots,
        "team_nodes": team_nodes,
        "team_slots": {k: slots[n] for k, n in team_nodes.items()},
        "team_identifiers": identifiers,
        "satellites": satellites,
        "jump_mgmt_ips": {s["name"]: s["jump_mgmt_ip"] for s in satellites},
        "probe_summary": {
            r.name: {k: probes[r.name].get(k)
                     for k in ("ok", "reasons", "missing_templates", "collisions",
                               "mem_free_bytes", "datastore_free", "running_ours")}
            for r in records},
        "computed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return placement


def placement_decision_table(placement, probes=None):
    """Human-readable placement summary for the deploy log / --plan-only preview."""
    probes = probes or placement.get("probe_summary") or {}
    lines = ["  Multi-node placement:"]
    eng = placement["engine_node"]
    eng_teams = teams_on_node(placement, eng)
    lines.append(f"    engine: {eng} (slot 0) — teams {', '.join(eng_teams) or '(none)'}")
    for sat in placement["satellites"]:
        lines.append(f"    satellite: {sat['name']} (slot {sat['slot']}) — "
                     f"teams {', '.join(sat['teams'])}, jump {sat['jump_mgmt_ip']} "
                     f"(vmid {sat['jump_vmid']})")
    for name, p in probes.items():
        mem = p.get("mem_free_bytes")
        mem_s = f"{mem / 1024 ** 3:.0f}G free" if mem else "mem unknown"
        extra = "" if p.get("ok") else f"  [!] {'; '.join(p.get('reasons') or [])}"
        lines.append(f"    probe {name}: {mem_s}, {p.get('running_ours', '?')} tezcatlipoca "
                     f"VM(s) running{extra}")
    return "\n".join(lines)


# ---------------------------------------------------------------- tfvars helpers

def satellite_tfvars(placement):
    """Per-slot provider settings for terraform (tfvars.json): exactly MAX_SATELLITES
    entries; unused slots get dummies so the aliased providers never configure. Token
    values resolve from env at write time (tfvars.json is 0600)."""
    out = []
    records = {r.name: r for r in (NodeRecord.from_json(v)
                                   for v in placement["nodes"].values())}
    by_slot = {v: k for k, v in placement["slots"].items()}
    for i in range(1, MAX_SATELLITES + 1):
        name = by_slot.get(i)
        if name:
            rec = records[name]
            token = os.environ.get(rec.token_env)
            if not token:
                raise SystemExit(f"  ERROR: token env '{rec.token_env}' for node "
                                 f"'{name}' is empty — add it to .env")
            out.append({"endpoint": rec.endpoint, "api_token": token, "node": rec.node,
                        "datastore": rec.datastore})
        else:
            # bpg configures every DECLARED provider even with zero resources, so the
            # dummy must pass token-format validation (it never connects).
            out.append({"endpoint": f"https://sat{i}.invalid",
                        "api_token": "root@pam!dummy=00000000-0000-0000-0000-000000000000",
                        "node": "unused", "datastore": "unused"})
    return out


def satellite_routes_for(placement):
    """Engine static routes: one per TEAM SUBNET behind each satellite's jump (not
    just the golden anchor — every team box on the satellite is reached through the
    same jump, and the engine's default gw blackholes the un-routed subnets).
    Written into the engine by the team_nics provisioner (trigger-keyed, so changed
    routes re-apply on the next apply #1)."""
    routes = []
    for sat in placement["satellites"]:
        anchor = sat.get("anchor_identifier")
        if anchor:
            routes.append({"subnet": f"192.168.{anchor}.0/24", "via": sat["jump_mgmt_ip"]})
        for k in sat.get("teams") or []:
            ident = placement["team_identifiers"][k]
            route = {"subnet": f"192.168.{ident}.0/24", "via": sat["jump_mgmt_ip"]}
            if route not in routes:
                routes.append(route)
    return routes


# ---------------------------------------------------------------- resolution entry

def resolve_placement(comp_dir, engine_vmid, teams, boxes, comp_name,
                      team_overrides=None, engine_override=None, resume_endpoint=None):
    """(placement, engine_record) or (None, None).

    Order: an existing placement.json always wins (authoritative for resume/redeploy
    and every companion op). Otherwise nodes.json triggers a fresh capacity-fill
    compute — except on a resume with no placement.json, where the range already
    lives on resume_endpoint: re-balancing a live range would strand it, so all
    teams adopt that node as a single-slot placement instead. Neither config present
    => single-node legacy (None)."""
    placement = read_placement(comp_dir)
    if placement:
        if set(placement["team_nodes"]) != set(teams):
            raise SystemExit(
                f"  ERROR: placement.json was computed for teams "
                f"{sorted(placement['team_nodes'])} but this deploy has teams "
                f"{sorted(teams)} — the placement (and its satellite routing) no "
                "longer matches. Destroy and redeploy to re-place.")
        if team_overrides or engine_override:
            print("  placement.json exists — ignoring --team-node/--engine-node "
                  "(destroy and redeploy to re-place)")
        return placement, engine_record(placement)
    records, balancing = load_nodes_config()
    if records is None:
        return None, None
    zero_base = [r.name for r in records if r.engine_base_vmid == 0]
    if zero_base:
        raise SystemExit(f"  ERROR: nodes.json node(s) {zero_base} lack engine_base_vmid")

    if resume_endpoint:
        match = next((r for r in records
                      if r.endpoint.rstrip("/") == resume_endpoint.rstrip("/")), None)
        if match is None:
            raise SystemExit(
                f"  ERROR: this competition's state records deploy endpoint "
                f"{resume_endpoint}, which is not in nodes.json — add that node or "
                "destroy and redeploy. Refusing to re-place a live range.")
        print(f"  Resume without placement.json — keeping the whole range on "
              f"'{match.name}' ({resume_endpoint})")
        probes = {r.name: probe_node(r, sorted({b["template"] for b in boxes}),
                                     engine_vmid,
                                     sorted({str(t["identifier"]) for t in teams.values()},
                                            key=int))
                  for r in records}
        placement = compute_placement(
            records, balancing, teams, boxes, engine_vmid, comp_name,
            team_overrides={k: match.name for k in teams},
            engine_override=match.name, probes=probes)
        write_placement(comp_dir, placement)
        return placement, engine_record(placement)

    print("  Multi-node config found (nodes.json) — probing nodes and placing teams...")
    probes = {r.name: probe_node(
        r, sorted({b["template"] for b in boxes}), engine_vmid,
        sorted({str(t["identifier"]) for t in teams.values()}, key=int))
        for r in records}
    placement = compute_placement(records, balancing, teams, boxes, engine_vmid,
                                  comp_name, team_overrides=team_overrides,
                                  engine_override=engine_override, probes=probes)
    print(placement_decision_table(placement, probes))
    write_placement(comp_dir, placement)
    print(f"  Placement written to {Path(comp_dir) / 'placement.json'} "
          "(authoritative for resume/redeploy/verify/destroy)")
    return placement, engine_record(placement)
