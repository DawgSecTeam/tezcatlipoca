# Remote access — participants reach their own team network over headscale

Every deploy enrolls the scoring engine as a **headscale subnet router** and hands each
participant a key that can reach **only their own team's `192.168.<identifier>.0/24`** plus the
scoreboard. Pre-existing tailnet users (the LDAP-backed accounts) keep tailnet-wide access through
`group:full-access` → `*`; limiting them by groups is deliberately deferred.

Control plane: the estate's headscale instance (URL, host and credentials:
`infrastructure-status.md`). Version and CLI quirks are
[environment-facts.md](environment-facts.md#headscale) — not repeated here.

## The mapping

`net/remote_access_ops.py` is the only writer of the ACL policy; `render_policy` rebuilds the
whole thing from scratch on every deploy and teardown that touches remote access.

| Range object | Headscale object |
|---|---|
| Engine (per deploy) | node `eng-<comp-id>`, tag `tag:range-router`, advertising every team `/24` |
| Participant person | user `comp-<id>-<person>` + single-use UNTAGGED preauth key (TTL `headscale.key_ttl`) |
| Person → team | `people.json` roster in the comp dir |
| Team `<id>` grant | the person's `@`-suffixed username → that `/24` (all ports) + the scoreboard `:80` |
| Pre-existing users | `group:full-access` → `*` |

One router advertises ALL team subnets — per-team router VMs are not needed: tailscaled on the
engine enforces the ACL per destination subnet before a packet ever reaches a team bridge.

**Why usernames, not tags.** A node enrolled with a tagged key gets its netmap routes filtered
correctly, but headscale never expands tag sources into the routers' packet filters — every
tag-sourced dial black-holes at the router while user/group-sourced dials pass. So participant
devices enroll UNTAGGED (identity is the person's user) and each team's grant names its people.

## Traffic path

```
participant laptop (100.64.x) ─ WireGuard/DERP → engine tailscale0
  → engine FORWARD (accept: 100.64/10 → 192.168.<id>.0/24)
  → SNAT to 192.168.<id>.1          ← boxes trust SSH only from the gateway IP
  → team box
```

The SNAT is the load-bearing piece (`remote_access_firewall_script`): box sshd accepts
gateway-IP auth only, so participant traffic must arrive as `192.168.<id>.1` — the same
treatment the jump VMs give routed-red traffic. **Satellite teams** SNAT to the *engine's mgmt
IP* instead; the satellite jump (which owns `.1` there) re-SNATs to the gateway with its existing
engine-source rule.

tailscaled re-applies its ruleset whenever an interface gains an address, which takes the
POSTROUTING chain with it, so a per-deploy `range-remote-access-monitor.service` watches
`ip monitor address link` and re-asserts on the event; `range-remote-access.timer` re-asserts as
the fallback (Docker also wipes iptables on restart). The engine template is untouched — no
template hash churn.

Isolation stays triple-layered: the headscale ACL (enforced on the engine's tailscale), the
engine's `192.168/16 → 192.168/16` FORWARD DROP, and the portless team bridges.

## What a deploy does (phase 3, `net/remote_access_ops.py::setup_remote_access`)

1. Preflight (`infra/gates.py::remote_access_gate`): `TEZ_HEADSCALE_*` present and the headscale
   host reachable — remote access ships with every deploy, so a dead control plane must stop the
   deploy at the gate, not mid-phase-3. The subnet-overlap refusal is the deploy's own next step.
2. **Refuse** a live router elsewhere advertising an overlapping `/24`: two routers on one `/24`
   are one HA route group and would cross-route each other's participants. Use disjoint
   `TEZ_TEAM_IDENTIFIERS` or tear the other range down.
3. Enroll/update the engine as the router (routes auto-approve via `autoApprovers`).
4. Push `range-remote-access.sh` + the monitor service and timer.
5. Create `comp-<id>-<person>` users + single-use preauth keys for everyone in `people.json`, then
   re-apply the whole ACL policy (the control plane restarts for seconds; the WireGuard data
   plane rides through).
6. Write `competitions/<id>/remote-access/` — `enrollment.md` (per-person
   `tailscale up --login-server … --authkey …` commands) and `keys.json` (gitignored, 0600). The
   student portal surfaces the same enrollment ([portal.md](portal.md)).

Teardown (`destroy-competition.py`) deletes the engine's headscale node (revoking its key),
then **each participant's devices before their users** — headscale refuses `users destroy` while a
user still owns nodes — then the comp's participant users, and re-applies the policy without this
comp. Warn-and-proceed like the rest of teardown.

## Running it

- **Opt out** per comp: `remote_access 0` in the Compfile (practice/capacity runs that must not
  touch the production tailnet).
- **Roster**: absent `people.json` = no participant grants; the router still enrolls and
  pre-existing users keep full access to the range.
- **Verify**: `verify-competition.py` runs the `remote_access` gate (engine enrolled, subnets
  SERVING, SNAT lines present, policy scoped). Proving what a participant can/cannot reach needs
  a real enrolled device — enroll one from `enrollment.md`, then check: own-team SSH works, other
  team's `/24` times out, `10.0.0.0/24` unreachable except the scoreboard `:80`.
- **Full access membership**: edit `full_access` in the headscale host's
  `tezcatlipoca-remote-access.json` policy state (the next deploy/teardown re-applies it). New
  LDAP users must be added there until the limiting-by-groups work lands.