# Multi-node: one competition across multiple Proxmox hosts

A competition can span up to 5 hosts (`MAX_SATELLITES + 1`): the **engine node** hosts the
scoring engine, the engine template, and its local teams; each **satellite** hosts some teams,
that slot's copies of the golden templates, and a small Alpine **jump/router VM** that
impersonates the engine's gateway IP there. Config lives in `nodes.json` (committed, no
secrets); the resolved choice lives in the state document's `placement` section (authoritative for
every later op). Each host is reached through a per-node route table
(`infra/client.py:register_node_routes`, keyed by PVE node name);
`multinode/placement_record.activate_placement` points the process config at the engine's host
and registers those routes, so every node-scoped API call goes to the host that owns it.

Since placement schema v2 that section also records every identity the run will create —
`engine_template_vmid`, `golden_vmids` per node, the engine management IP and gateway, the
management bridge, and red01's `{vmid, ip, node, explicit_ip}`. `prepare()` writes them once,
before preflight reads anything, so preflight, resume, verify and teardown derive no vmid
independently. A v1 section is upgraded on read — red01 resolved by the v1 derivation, from the
Compfile or the comp's own bad-auto config — and written back, so a teardown run by newer code
can still find the red01 an older deploy left live.

## Why routing, not bridging

Bridges are node-local L2. The engine needs `192.168.<id>.1` on every team bridge (scoring
probes, nakon, NAT/isolation, and boxes' gateway-IP SSH trust all point there), so a team on
another host needs that address to exist there too. The jump VM provides it: it holds
`192.168.<id>.1` for its local teams, DNATs the gateway's apt-cacher port to the engine, and
SNATs engine→box traffic so the boxes' gateway-only trust still works. Static routes on the
engine (`192.168.<anchor>.0/24 via <jump mgmt IP>`) close the path. Nothing changes from a box's
perspective — same gateway, DNS, apt proxy, scoring.

Isolation keeps two enforcement points with the same rule shape: the engine's team-to-team DROP
still applies to routed satellite traffic, and the jump's FORWARD policy is DROP with accepts
only for established, engine→its teams, teams→engine, and teams→internet.

## Routed red

Under bad-auto's `routed` mode (default), red01 sits on its own segment (`deploy.red_subnet`,
default `10.200.0.0/24`) on a **private per-run bridge** (`vmbrR<run hex>`, created by the deploy;
the engine gets an extra NIC there holding that segment's gateway). Red's source is preserved
so blue can see and firewall the attacker. The private bridge is what keeps the segment's fixed
`.1`/`.10` addresses unique: on the shared management bridge a second concurrent range's engine
would answer red01's ARP for the gateway and red's team traffic would go to an engine with no
route to those subnets. That covers engine-node
teams only — satellite teams are reachable only through the jump, whose FORWARD policy is DROP
by default, so red's source matches no rule there.

Set `knobs.red_segment` (or `TEZ_RED_SEGMENT`) to the same CIDR as the comp's bad-auto
`deploy.red_subnet`. Each jump then accepts red→its local teams, accepts replies, and SNATs red
to `192.168.<id>.1` so the gateway-only SSH trust holds. Leave unset for single-node ranges and
`masq`-mode red, which shares the team gateways and needs no path of its own.

The deploy refuses to start an event when this is wrong: `run-agent-scrim` runs
`verify-competition.py --red-identity --red-teams all` in `stage_red` before T0, failing the run
if red01 can't dial a box on every team. Check by hand:

```bash
python3 verify-competition.py competitions/<id> --red-ip <red mgmt IP> \
    --red-identity --red-teams all
```

`--red-identity` alone only proves red reaches one box on its own segment — always pass
`--red-teams all` on a multi-node range.

## `nodes.json`

The node list is a per-run input: `--nodes <path>` points at it (the dashboard writes one
per run from its node catalog). Without `--nodes` the cwd `nodes.json` is used; with
neither, the run is single-node from the ambient config, and an explicit `--engine-node`/`--team-node`
is refused rather than silently dropped. The file itself carries no secrets — `token_env`
names the env var holding that host's token. Real per-host values (endpoint, PVE node,
datastore, engine base vmid, ssh host): `infrastructure-status.md`.

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

- `token_env` names the env var holding that host's API token (tokens never live in this
  file) and **must be unique per record**: `activate_placement` reads each record's named variable
  and registers that host's endpoint/token route (the engine record's also becomes the in-process
  config override), so two records sharing one name route every call with the same token.
  `load_nodes_config` refuses duplicates.
- `node` is the PVE host name; the API route table keys on it.
- `engine_base_vmid` is that host's engine base template (hosts differ).
- `jump_mgmt_ip` pins the satellite jump's mgmt address; empty walks down from `10.0.0.249` per
  slot. Preflight sweeps running guests' agent IPs on every hosting node and refuses collisions —
  assign explicitly on a shared mgmt LAN.
- `mgmt_bridge` is that node's management bridge (default: the profile's `proxmox.mgmt_bridge`,
  else `vmbr0`). The engine, the engine-template build VM and the node's jump VM all attach their
  `net0` there, and preflight refuses a bridge the node does not own.
- `weight` divides a node's free RAM (bigger weight = counts for less); `max_teams` caps how many
  teams capacity-fill stacks on a node.

**Preflight** is not a separate code path: `run_preflight` builds one `NodeShare` per hosting node
(engine node: base image + slot-0 goldens + its teams; satellite: its slot's goldens, jump VM,
its teams, a jump clone source) and runs the same template/collision/headroom checks as a
single-node, one-share plan, then the mgmt-IP sweeps and catalog check. `in_path` firewalls on
satellite teams are refused while building the plan. Placement code:
`multinode/nodes_config.py` / `placement_record.py` / `placement_planner.py` (no facade; import
from the owning module). Absent `nodes.json` → every op behaves exactly as single-node.

## Placement

`create-competition.py ... [--team-node 150=zfs-193,152=zfs-193] [--engine-node hdd-150]`:

1. **Probe** each node: reachable, free RAM, datastore free, per-node template map, vmid/bridge
   collision sweep (engine + engine-template + every slot's golden span + jump slots + every
   candidate team block).
2. **Teams** pack biggest-first onto the eligible node with the most effective free RAM
   (free/weight minus already-placed box memory plus a 512 MB jump reservation per satellite). A
   node is eligible only if every box template exists on it. Overrides win outright.
3. **Engine node** = override, else the node holding the most teams (tie: `prefer` order, then
   free RAM). Must have `engine_base_vmid`.
4. The result prints as a decision table and is written into the state document. From then on it is
   authoritative: resume, redeploy, verify, destroy all read it back, so a re-run balancer or an
   ambient config swap can never move ops onto the wrong host. `--team-node`/`--engine-node` are ignored
   once a placement is recorded (destroy first to re-place).
5. A resume with no recorded placement adopts its recorded `deployed_endpoint` as a single-slot
   placement instead of re-balancing a live range.

## vmid / identity scheme (slot-dimensioned)

| Thing | vmid | Where |
|---|---|---|
| Team boxes | `200 + id*10 + idx` (unchanged) | the team's node |
| Golden per slot | `engine_vmid + 150 + slot*10 + box_idx` | slot's node |
| Jump VM | `engine_vmid + 130 + slot` | satellite node |
| Engine template | `engine_vmid + 140` | engine node |

Each satellite builds its own golden set (linked clones can't cross hosts on separate storages)
with identical planted content; hashes are slot-independent, so reuse/rebuild works per node.

## What deploy does differently

- **Phase 1**: the interrupted-clone reclaim scans every hosting node, filtered to this deploy's
  slots (each satellite's jump VM and slot-shifted goldens included; no engine there). Destruction
  of a leftover range is preflight's refusal on any node, reclaimed by `destroy-competition.py`.
- **Phase 2**: the estate component's create step builds bridges on every host, the engine, and the
  engine's satellite routes (persisted as a systemd oneshot so an engine reboot re-asserts them);
  jump VMs clone and configure concurrently on a bounded pool of 4 (cloud-init static IPs,
  iptables-restore ruleset; one that never comes up aborts the deploy), then `routing_ops`
  fail-loud-gates engine→jump reachability before anything depends on it.
- **Phase 4**: builds the golden set on the engine node as usual, then once per satellite
  (anchored on its first local team subnet, planted through the routed path); the box build then
  clones team boxes per slot from that slot's goldens.
- **Phases 5–7** are unchanged — everything rides the engine and is routed.

`--plan-only` previews placement decisions without touching infra.

## Teardown and companion ops

`destroy-competition.py`, `verify-competition.py`, `redeploy-competition.py` activate the
placement record first; per-target ops (snapshots, rollbacks, rebuilds, pre-stop) go to the
target's own node. The default full teardown destroys every slot's golden set and the jump VMs
in addition to the usual.

## Template copies

Satellites need their own copies of every box template plus an Alpine base (the jump clone
source, name-matched by `jump_template`, default `alpine`):

```bash
python3 sync-template.py --template base-ubuntu24.04-fix --from hdd-150 --to zfs-193 [--dry-run]
```

(`vzdump`→`qmrestore` over root SSH; nodes must accept root SSH keys — the API-only deploy path
itself never needs node SSH.)

This is the one operation with no API-only form: the API refuses `vzdump`'s `stdout` (`option is
only allowed on the command line interface`) and exposes no streamed export or import, so the dump
cannot leave the source node through it. The restore half *is* API-capable (`POST
/nodes/{node}/qemu` takes `archive=<path|volume>`), so a datastore both nodes see would make a
sync possible — dump to that storage, restore from its volume id. Until one exists, a host without
root SSH gets a template by building it on that host, not by syncing it.

## Invariants

- A competition's teams may span nodes, but the engine, engine template, and routing pivot are
  singular and always co-located with slot 0.
- vmids only need uniqueness per PVE instance; the slot stride keeps the scheme collision-safe
  even on a future single-cluster backend.
- The recorded placement beats the ambient config, `nodes.json`, and CLI pins for every op against an
  already-placed competition.
- Engine and jump mgmt IPs are swept against running guests on all hosting nodes — assign
  explicitly on a shared mgmt LAN when other engines hold the defaults.
