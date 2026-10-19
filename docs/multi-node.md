# Multi-node: one competition across multiple Proxmox hosts

A competition can span up to `MAX_SATELLITES + 1` hosts (`multinode/nodes_config.py`): the
**engine node** hosts the scoring engine, the engine template, and its local teams; each
**satellite** hosts some teams, its own copies of the golden templates, and a small Alpine
**jump/router VM** holding the engine's gateway IP locally. The placement is decided once by
`prepare()` into the state document — which is authoritative for every later op, so preflight,
resume, verify and teardown never derive a vmid independently.
`multinode/placement_record.activate_placement` points the process config at the engine's host and
registers per-node routes (`infra/client.py:register_node_routes`), so every node-scoped API call
goes to the host that owns it.

## Why routing, not bridging

Bridges are node-local L2, and the engine needs `192.168.<id>.1` on every team bridge — scoring,
nakon, NAT/isolation and the boxes' gateway-IP SSH trust all point there. The jump VM provides that
address on its host: it holds the gateway, DNATs apt-cacher to the engine, and SNATs engine→box
traffic, so from a box's perspective nothing changes. The engine's team-to-team DROP still applies
to routed traffic ([internals.md](internals.md#multi-node-and-sync)).

## Routed red

Red keeps its source address end-to-end so blue can see and firewall the attacker
([architecture.md](architecture.md#network-and-isolation)). Satellite teams are reachable only
through the jump (FORWARD DROP by default), which accepts red→its teams and replies, and SNATs red
to the team gateway so gateway-only SSH trust holds. The red CIDR comes from the single
`common/constants.py` `RED_SEGMENT_SUBNET` via `net/jump_rules.red_segment_from_env()` — engine,
jumps and verifier cannot disagree about it. Red's WAN egress is masqueraded at the jump, so **its
source identity survives only on engine-node teams**.

The scrim refuses to start an event on a broken red path: `stage_red` runs the all-team identity
check before T0. Check by hand:

```bash
python3 verify-competition.py competitions/<id> --red-ip <red mgmt IP> \
    --red-identity --red-teams all
```

`--red-identity` alone only proves red reaches one box on its own segment — always pass
`--red-teams all` on a multi-node range.

## `nodes.json`

A per-run input: `--nodes <path>` points at it (the dashboard writes one per run from its node
catalog). Without `--nodes` the cwd `nodes.json` is used; with neither, the run is single-node from
the ambient config and an explicit `--engine-node`/`--team-node` is **refused rather than silently
dropped**. The file shape is the `NodeRecord` fields parsed and validated by
`multinode/nodes_config.py:load_nodes_config` — no secrets in the file; real per-host values
(endpoint, PVE node, datastore, engine base vmid, ssh host): `infrastructure-status.md`. Three keys
carry a rule an operator can get wrong:

- **`token_env` must be unique per record** — two records sharing one name route every call with
  the same token; `load_nodes_config` refuses duplicates.
- **`jump_mgmt_ip`** pins the satellite jump's mgmt address; empty walks down from
  `JUMP_MGMT_IP_BASE` per slot. Preflight sweeps running guests' agent IPs on every hosting node
  and refuses collisions — assign explicitly on a shared mgmt LAN.
- **`mgmt_bridge`** is that node's management bridge (`placement_record.mgmt_bridge_for()`: the
  record's own, else the profile's). The engine, the engine-template build VM and the node's jump
  VM attach `net0` there; preflight refuses a bridge the node does not own.

`in_path` firewalls on satellite teams are refused while building the plan.

## Placement

`create-competition.py ... [--team-node 150=<node-b>,152=<node-b>] [--engine-node <node-a>]`
(`--plan-only` previews the decisions without touching infra).
`multinode/placement_planner.py` probes each node and packs teams by capacity-fill, then picks the
engine node. Two eligibility rules worth knowing: a node is only eligible if **every box template
exists on it**, and the engine node must have an `engine_base_vmid`. Overrides (`--team-node`,
`--engine-node`) win outright.

The decision table printed at deploy is written into the state document, where it is authoritative:
resume, redeploy, verify and destroy all read it back, so **the node pins are ignored once a
placement is recorded** (destroy first to re-place). A resume with no recorded placement adopts its
recorded `deployed_endpoint` as a single-slot placement rather than re-balancing a live range.

## vmid scheme

`common/constants.py`'s offsets (`GOLDEN_VMID_OFFSET`, `ENGINE_TEMPLATE_VMID_OFFSET`) and
`multinode/nodes_config.JUMP_VMID_OFFSET` applied over the engine vmid, plus
`common/targets.vm_id_for()` for team boxes — slot stride folded in by
`placement_record.golden_vmid_for_slot()` / `jump_vmid_for()`. **Each satellite builds its own
golden set** (linked clones can't cross hosts on separate storages) with identical planted content;
hashes are slot-independent, so template reuse/rebuild works per node.

## Deploy differences that matter

Bridges are built on every host and the engine's satellite routes persist across an engine reboot;
a jump VM that never comes up **aborts the deploy**, and engine→jump reachability is fail-loud
gated before anything depends on it. The golden set builds once per node — satellites anchor on
their first local team subnet and plant through the routed path; team boxes clone from their own
slot's goldens. Phases 5–7 are unchanged — everything rides the engine and is routed.

## Teardown and companion ops

`destroy-competition.py`, `verify-competition.py`, `redeploy-competition.py` activate the placement
record first; per-target ops (snapshots, rollbacks, rebuilds, pre-stop) go to the target's own
node. The default full teardown destroys every slot's golden set and the jump VMs in addition to
the usual.

## Template copies

Satellites need their own copies of every box template plus an Alpine base (the jump clone source,
name-matched by `jump_template`, default `alpine`):

```bash
python3 sync-template.py --template <box-template> --from <node-a> --to <node-b> [--dry-run]
```

This is the one operation with no API-only form: the API refuses `vzdump`'s `stdout`, so the dump
cannot leave the source node through it — a manual move is `vzdump` → transfer (keeping the vzdump
filename pattern) → `qmrestore`. **Nodes must accept root SSH keys** — the API-only deploy path
itself never needs node SSH. A host without it gets a template by building it there, not by
syncing.

## Invariants

- The engine, engine template and routing pivot are singular and always co-located with slot 0;
  red01 is on the engine node, the jumps carry the red segment to satellite teams.
- The recorded placement beats the ambient config, `nodes.json` and CLI pins for every op against
  an already-placed competition.
- Engine and jump mgmt IPs are swept against running guests on all hosting nodes — assign
  explicitly on a shared mgmt LAN when other engines hold the defaults
  ([environment-facts.md](environment-facts.md#vmid-occupancy)).
