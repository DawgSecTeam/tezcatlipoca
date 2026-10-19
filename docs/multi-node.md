# Multi-node: one competition across multiple Proxmox hosts

A competition can span up to `MAX_SATELLITES + 1` hosts: the **engine node** hosts the scoring
engine, the engine template, and its local teams; each **satellite** hosts some teams, that slot's
copies of the golden templates, and a small Alpine **jump/router VM** that impersonates the engine's
gateway IP there. Config lives in `nodes.json` (committed, no secrets); the resolved choice lives in
the state document's `placement` section, which is authoritative for every later op. Each host is
reached through a per-node route table keyed by PVE node name
(`infra/client.py:register_node_routes`); `multinode/placement_record.activate_placement` points the
process config at the engine's host and registers those routes, so every node-scoped API call goes to
the host that owns it.

The placement section records every identity the run will create — engine template vmid, `golden_vmids`
per node, engine management IP/gateway/bridge, and red01's `{vmid, ip, node, explicit_ip}` — written
once by `prepare()` before preflight reads anything, so preflight, resume, verify and teardown derive
no vmid independently. A record written by an older schema version is upgraded on read (red01
re-resolved by `red/red01_ops.py`) and written back, so newer teardown code can still find a red01 an
older deploy left live.

## Why routing, not bridging

Bridges are node-local L2, and the engine needs `192.168.<id>.1` on every team bridge — scoring
probes, nakon, NAT/isolation and the boxes' gateway-IP SSH trust all point there. So a team on another
host needs that address to exist there too, which the jump VM provides: it holds the gateway address
for its local teams, DNATs the gateway's apt-cacher port to the engine, and SNATs engine→box traffic
so gateway-only trust still holds. From a box's perspective nothing changes — same gateway, DNS, apt
proxy, scoring. The engine's team-to-team DROP still applies to routed traffic, so isolation keeps
two enforcement points with the same rule shape ([internals.md](internals.md#multi-node-and-sync)).

## Routed red

Red keeps its source address end-to-end so blue can see and firewall the attacker
([architecture.md](architecture.md#network-and-isolation)). Satellite teams are reachable only through
the jump, whose FORWARD policy is DROP by default, so each jump carries the red path for its local
teams: it accepts red → its teams, accepts the replies, and SNATs red to the team gateway so the
gateway-only SSH trust holds. The jump rules take the CIDR from
`net/jump_rules.red_segment_from_env()` — the one `RED_SEGMENT_SUBNET` constant the engine, the jumps
and the verifier cannot disagree about. Red's WAN egress is masqueraded there, so **its source
identity survives only on engine-node teams**.

The deploy refuses to start an event when this is wrong: `run-agent-scrim` runs
`verify-competition.py --red-identity --red-teams all` in `stage_red` before T0, failing the run if
red01 can't dial a box on every team. Check by hand:

```bash
python3 verify-competition.py competitions/<id> --red-ip <red mgmt IP> \
    --red-identity --red-teams all
```

`--red-identity` alone only proves red reaches one box on its own segment — always pass
`--red-teams all` on a multi-node range.

## `nodes.json`

A per-run input: `--nodes <path>` points at it (the dashboard writes one per run from its node
catalog). Without `--nodes` the cwd `nodes.json` is used; with neither the run is single-node from the
ambient config, and an explicit `--engine-node`/`--team-node` is **refused rather than silently
dropped**. No secrets live in the file. Real per-host values (endpoint, PVE node, datastore, engine
base vmid, ssh host): `infrastructure-status.md`.

```json
{
  "balancing": {"strategy": "capacity-fill", "prefer": ["<record-name>"]},
  "nodes": [
    {"name": "<record-name>",
     "endpoint": "https://<host>:8006",
     "token_env": "TEZ_PROXMOX_API_TOKEN",
     "node": "<pve-node>", "datastore": "<datastore>",
     "engine_base_vmid": 0,
     "engine_mgmt_ip": "", "jump_mgmt_ip": "", "mgmt_bridge": "",
     "ssh_host": "<host>", "jump_template": "alpine",
     "weight": 1.0, "max_teams": 8}
  ]
}
```

Three keys carry a rule an operator can get wrong:

- **`token_env` must be unique per record.** `activate_placement` reads each record's named variable
  and registers that host's endpoint/token route (the engine record's also becomes the in-process
  config override), so two records sharing one name route every call with the same token.
  `load_nodes_config` refuses duplicates.
- **`jump_mgmt_ip`** pins the satellite jump's mgmt address; empty walks down from
  `JUMP_MGMT_IP_BASE` per slot. Preflight sweeps running guests' agent IPs on every hosting node and
  refuses collisions — assign explicitly on a shared mgmt LAN.
- **`mgmt_bridge`** is that node's management bridge (`placement_record.mgmt_bridge_for()` resolves it:
  the record's own, else the profile's). The engine, the engine-template build VM and the node's jump
  VM all attach their `net0` there, and preflight refuses a bridge the node does not own.

The rest are descriptive: `node` is the PVE host name the route table keys on, `engine_base_vmid` is
that host's engine base template, and `weight`/`max_teams` shape capacity-fill.

**Preflight is not a separate code path**: `run_preflight` builds one `NodeShare` per hosting node and
runs the same template/collision/headroom checks a single-node plan runs, then the mgmt-IP sweeps and
catalog check. `in_path` firewalls on satellite teams are refused while building the plan. Placement
code: `multinode/nodes_config.py` / `placement_record.py` / `placement_planner.py` (no facade; import
from the owning module). Absent `nodes.json` → every op behaves exactly as single-node.

## Placement

`create-competition.py ... [--team-node 150=<node-b>,152=<node-b>] [--engine-node <node-a>]`

`multinode/placement_planner.py` probes each node (reachability, free RAM, datastore, the per-node
template map, and a vmid/bridge collision sweep over every band this placement would take), packs
teams biggest-first onto the node with the most effective free RAM, then picks the engine node. Two
eligibility rules worth knowing: a node is only eligible if **every box template exists on it**, and
the engine node must have `engine_base_vmid`. Overrides (`--team-node`, `--engine-node`) win outright.

The result prints as a decision table and is written into the state document, where it is
authoritative: resume, redeploy, verify and destroy all read it back, so a re-run balancer or an
ambient config swap can never move ops onto the wrong host — **the node pins are ignored once a
placement is recorded** (destroy first to re-place). A resume with no recorded placement adopts its
recorded `deployed_endpoint` as a single-slot placement rather than re-balancing a live range.

## vmid / identity scheme (slot-dimensioned)

The scheme is `common/constants.py`'s offsets (`GOLDEN_VMID_OFFSET`, `ENGINE_TEMPLATE_VMID_OFFSET`,
`multinode/nodes_config.JUMP_VMID_OFFSET`) applied over the engine vmid, plus
`common/targets.vm_id_for()` for team boxes — with the slot stride folded in by
`placement_record.golden_vmid_for_slot()` / `jump_vmid_for()`. **Each satellite builds its own
golden set** (linked clones can't cross hosts on separate storages) with identical planted content;
hashes are slot-independent, so reuse/rebuild works per node.

## What deploy does differently

- **Phase 1**: the interrupted-clone reclaim scans every hosting node, filtered to this deploy's
  slots (each satellite's jump VM and slot-shifted goldens; no engine there).
- **Phase 2**: bridges are built on every host, and the engine's satellite routes persist as a systemd
  oneshot so an engine reboot re-asserts them. Jump VMs build on a bounded pool and one that never
  comes up **aborts the deploy**; `routing_ops` then fail-loud-gates engine→jump reachability before
  anything depends on it.
- **Phase 4**: the golden set is built once per node — on the engine node as usual, then on each
  satellite anchored on its first local team subnet and planted through the routed path. Team boxes
  then clone from their own slot's goldens.
- **Phases 5–7** are unchanged — everything rides the engine and is routed.

`--plan-only` previews placement decisions without touching infra.

## Teardown and companion ops

`destroy-competition.py`, `verify-competition.py`, `redeploy-competition.py` activate the placement
record first; per-target ops (snapshots, rollbacks, rebuilds, pre-stop) go to the target's own node.
The default full teardown destroys every slot's golden set and the jump VMs in addition to the usual.

## Template copies

Satellites need their own copies of every box template plus an Alpine base (the jump clone source,
name-matched by `jump_template`, default `alpine`):

```bash
python3 sync-template.py --template <box-template> --from <node-a> --to <node-b> [--dry-run]
```

This is the one operation with no API-only form: the API refuses `vzdump`'s `stdout`, so the dump
cannot leave the source node through it. **Nodes must accept root SSH keys** — the API-only deploy path
itself never needs node SSH. A host without it gets a template by building it there, not by syncing.

## Invariants

- The engine, engine template and routing pivot are singular and always co-located with slot 0. red01
  is on the engine node; the jumps carry the red segment to satellite teams.
- The recorded placement beats the ambient config, `nodes.json` and CLI pins for every op against an
  already-placed competition.
- Engine and jump mgmt IPs are swept against running guests on all hosting nodes — assign explicitly
  on a shared mgmt LAN when other engines hold the defaults
  ([environment-facts.md](environment-facts.md#vmid-and-address-occupancy)).
