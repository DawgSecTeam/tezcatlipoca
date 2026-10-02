# Multi-node: one competition across multiple Proxmox hosts

A competition can span up to 5 hosts (`MAX_SATELLITES + 1`): the **engine node**
hosts the scoring engine, the engine template, and its local teams; each
**satellite** hosts some teams, that slot's copies of the golden templates, and a
small alpine **jump/router VM** that impersonates the engine's gateway IP there.
Config lives in `nodes.json` (committed, no secrets); the resolved choice lives in
`competitions/<id>/placement.json` (authoritative for every later op).

## Why routing, not bridging

Bridges are node-local L2. The engine needs `192.168.<id>.1` on every team bridge
(scoring probes, nakon, NAT/isolation, and boxes' gateway-IP SSH trust all point
there), so a team on another host needs *that address to exist on the other host*.
The jump VM provides it: it holds `192.168.<id>.1` for its local teams, DNATs the
gateway IP's apt-cacher port to the engine, and SNATs engine→box traffic so the
boxes' gateway-IP trust still works. Static routes on the engine
(`192.168.<anchor>.0/24 via <jump mgmt IP>`) close the path. From every box's
perspective nothing changes — same gateway, same DNS, same apt proxy, same scoring.

Isolation keeps two enforcement points with the same rule shape: the engine's
`192.168.0.0/16 → 192.168.0.0/16 DROP` still applies to routed satellite traffic,
and the jump's FORWARD policy is DROP with accepts only for established, engine→its
teams, teams→engine, and teams→outside-`192.168.0.0/16` (internet). Team↔team and
mgmt→team fall off the DROP.

## `nodes.json`

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

- `token_env` names the `.env` variable holding that host's API token — tokens
  never live in this file.
- `node` is the PVE host name; the API route table keys on it.
- `engine_base_vmid` is that host's engine base template (hosts differ).
- `jump_mgmt_ip` pins the satellite jump's mgmt address; empty walks down from
  `10.0.0.249` per slot. Preflight sweeps running guests' agent IPs on all hosting
  nodes and refuses collisions (live engines on a shared mgmt LAN make this a real
  hazard — assign explicitly when .245–.250 are taken).
- `weight` divides a node's free RAM (bigger weight = counts for less);
  `max_teams` caps how many teams capacity-fill will stack on a node.

Absent `nodes.json` → every deploy/op behaves exactly as the single-node design.

## Placement

`create-competition.py ... [--team-node 150=zfs-193,152=zfs-193]
[--engine-node hdd-150]`:

1. **Probe** each node: reachable, free RAM, datastore free, per-node template map,
   vmid/bridge collision sweep (engine + engine-template + every slot's golden span
   `+150..+189` + jump slots `+131..+134` + every candidate team block).
2. **Teams** pack biggest-first onto the eligible node with the most effective free
   RAM (free/weight minus Σ box memory of already-placed teams + a 512 MB jump
   reservation per satellite). A node is eligible for a team only if every box
   template exists on it. Overrides win outright.
3. **Engine node** = override, else the node holding the most teams (tie: `prefer`
   order, then free RAM). Must have `engine_base_vmid`.
4. The result prints as a decision table and is written to
   `competitions/<id>/placement.json`. From then on it is authoritative: resume,
   redeploy, verify, and destroy read it back — a re-run balancer or an `.env` swap
   can never move ops onto the wrong host. `--team-node`/`--engine-node` are
   ignored once placement.json exists (destroy first to re-place).
5. A resume without placement.json (a range deployed before this existed) adopts
   its recorded `deployed_endpoint` as a single-slot placement instead of
   re-balancing a live range.

## vmid / identity scheme (slot-dimensioned)

| Thing | vmid | Where |
|---|---|---|
| Team boxes | `200 + id*10 + idx` (unchanged) | the team's node |
| Golden per slot | `engine_vmid + 150 + slot*10 + box_idx` | slot's node |
| Jump VM | `engine_vmid + 130 + slot` | satellite node |
| Engine template | `engine_vmid + 140` | engine node |

Each satellite builds its own golden set (linked clones can't cross hosts on
separate storages) with identical planted content — the hashes are slot-independent,
so reuse/rebuild logic works per node.

## What deploy does differently

- **Phase 1** runs one destroy-wave pair per hosting node (satellite waves also
  destroy the jump VM and slot-shifted goldens; no engine there).
- **Phase 2** apply #1 builds bridges on every host, the engine, and the engine's
  satellite routes (persisted as a systemd oneshot so an engine reboot re-asserts
  them); then the jump VMs are cloned + configured **concurrently on a bounded pool of 4**
  (cloud-init static IPs, iptables-restore ruleset, `verify_jump`; each satellite is an
  independent host, and one that never comes up still aborts the deploy), and `routing_ops`
  fail-loud gates engine→jump reachability before anything depends on it.
- **Phase 4** builds the golden set on the engine node as today, then once per
  satellite (anchored on the satellite's first local team subnet, planted through
  the routed path); apply #2 builds team boxes per slot from that slot's goldens.
- **Phases 5–7** are unchanged — everything rides the engine and is routed.

`--plan-only` previews placement decisions without touching infra.

## Teardown & companion ops

`destroy-competition.py`, `verify-competition.py`, and `redeploy-competition.py`
activate the placement record first; per-target ops (snapshots, rollbacks,
rebuilds, Windows pre-stop) go to the target's own node. `--full` destroys every
slot's golden set and the jump VMs in addition to the usual.

## Template copies

Satellites need their own copies of every box template plus an alpine base (the
jump clone source — name-matched by `jump_template`, default `alpine`). Copy with:

```
python3 sync-template.py --template base-ubuntu24.04-fix --from hdd-150 --to zfs-193 [--dry-run]
```

(vzdump→qmrestore over root SSH; nodes must accept root SSH keys — the API-only
deploy path itself never needs node SSH.)

## Invariants

- A competition's teams may span nodes, but the engine, engine template, and the
  routing pivot are singular and always co-located with slot 0.
- vmids only need uniqueness per PVE instance; the slot stride keeps the scheme
  collision-safe even on a future single-cluster backend.
- `placement.json` beats `.env`, `nodes.json`, and CLI pins for every op against
  an already-placed competition.
- Engine mgmt IP and jump mgmt IPs are swept against running guests on all hosting
  nodes — on a shared mgmt LAN, assign them explicitly when other engines hold the
  defaults.
