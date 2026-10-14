# Multi-node: one competition across multiple Proxmox hosts

A competition can span up to 5 hosts (`MAX_SATELLITES + 1`): the **engine node** hosts the
scoring engine, the engine template, and its local teams; each **satellite** hosts some teams,
that slot's copies of the golden templates, and a small Alpine **jump/router VM** that
impersonates the engine's gateway IP there. Config lives in `nodes.json` (committed, no
secrets); the resolved choice lives in `competitions/<id>/placement.json` (authoritative for
every later op).

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
default `10.200.0.0/24`) with the engine holding that segment's gateway, forwarding red→teams
with the source preserved so blue can see and firewall the attacker. That covers engine-node
teams only — satellite teams are reachable only through the jump, whose FORWARD policy is DROP
by default, so red's source matches no rule there.

Set `TEZ_RED_SEGMENT` (`.env.example`) to the same CIDR as the comp's bad-auto
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
neither, the run is single-node from `.env`, and an explicit `--engine-node`/`--team-node`
is refused rather than silently dropped. The file itself carries no secrets — `token_env`
names the env var holding that host's token.

```json
{
  "balancing": {"strategy": "capacity-fill", "prefer": ["hdd-150"]},
  "nodes": [
    {"name": "hdd-150",
     "endpoint": "https://10.0.0.150:8006",
     "token_env": "TF_VAR_proxmox_api_token",
     "node": "proxmox", "datastore": "hdd",
     "engine_base_vmid": 955,
     "engine_mgmt_ip": "", "jump_mgmt_ip": "",
     "ssh_host": "10.0.0.150", "jump_template": "alpine",
     "weight": 1.0, "max_teams": 8}
  ]
}
```

- `token_env` names the `.env` variable holding that host's API token (tokens never live in this
  file) and **must be unique per record** — placement writes one record's token into the env var
  that record names, so two records sharing a name make the last-written one win for both.
  `load_nodes_config` refuses duplicates.
- `node` is the PVE host name; the API route table keys on it.
- `engine_base_vmid` is that host's engine base template (hosts differ).
- `jump_mgmt_ip` pins the satellite jump's mgmt address; empty walks down from `10.0.0.249` per
  slot. Preflight sweeps running guests' agent IPs on every hosting node and refuses collisions —
  assign explicitly on a shared mgmt LAN.
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
4. The result prints as a decision table and is written to `placement.json`. From then on it is
   authoritative: resume, redeploy, verify, destroy all read it back, so a re-run balancer or an
   `.env` swap can never move ops onto the wrong host. `--team-node`/`--engine-node` are ignored
   once `placement.json` exists (destroy first to re-place).
5. A resume without `placement.json` (a range deployed before it existed) adopts its recorded
   `deployed_endpoint` as a single-slot placement instead of re-balancing a live range.

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

- **Phase 1**: one destroy-wave pair per hosting node (satellite waves also destroy the jump VM
  and slot-shifted goldens; no engine there).
- **Phase 2**: apply #1 builds bridges on every host, the engine, and the engine's satellite
  routes (persisted as a systemd oneshot so an engine reboot re-asserts them); jump VMs clone and
  configure concurrently on a bounded pool of 4 (cloud-init static IPs, iptables-restore
  ruleset; one that never comes up aborts the deploy), then `routing_ops` fail-loud-gates
  engine→jump reachability before anything depends on it.
- **Phase 4**: builds the golden set on the engine node as usual, then once per satellite
  (anchored on its first local team subnet, planted through the routed path); apply #2 builds
  team boxes per slot from that slot's goldens.
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

## Invariants

- A competition's teams may span nodes, but the engine, engine template, and routing pivot are
  singular and always co-located with slot 0.
- vmids only need uniqueness per PVE instance; the slot stride keeps the scheme collision-safe
  even on a future single-cluster backend.
- `placement.json` beats `.env`, `nodes.json`, and CLI pins for every op against an
  already-placed competition.
- Engine and jump mgmt IPs are swept against running guests on all hosting nodes — assign
  explicitly on a shared mgmt LAN when other engines hold the defaults.
