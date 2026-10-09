# Remote access — participants reach their own team network over headscale

Every deploy enrolls the scoring engine as a **headscale subnet router** and hands each
participant a key that can reach **only their own team's `192.168.<identifier>.0/24`** plus the
scoreboard. Pre-existing tailnet users (the LDAP-backed accounts) keep the full access they had
when the tailnet ran with no policy at all; limiting them by groups is deliberately deferred.

Control plane: **`https://headscale.hnasheralneam.dev`** (headscale v0.29 in Docker behind
Caddy on a VPS; embedded DERP on the same host relays participant traffic). See
[environment-facts.md](environment-facts.md#headscale) for the ground truth.

## The mapping

| Range object | Headscale object |
|---|---|
| Engine (per deploy) | tailscale node `eng-<comp-id>`, tag `tag:range-router`, advertising every team `/24` |
| Participant person | user `comp-<id>-<person>` + single-use UNTAGGED preauth key (TTL `TEZ_REMOTE_KEY_TTL`, default 72h) |
| Person → team | `people.json` roster in the comp dir: `{"team1": ["alice"], ...}` |
| Team `<id>` grant | grants keyed on the usernames: `comp-<id>-<person>@` → `192.168.<id>.0/24` (all ports) + `10.0.0.<engine>` (port 80) |
| Pre-existing users | `group:full-access` → `*` (the policy-less allow-all they had before 2026-10-08) |

One router advertises ALL team subnets — per-team router VMs are not needed: tailscaled on the
engine enforces the ACL per destination subnet before a packet ever reaches a team bridge.

**Why usernames, not tags, key the participant grants** (live-proven 2026-10-08, headscale
v0.29.1): a node enrolled with a tagged key gets its netmap routes filtered correctly, but
headscale never expands tag sources into the ROUTERS' packet filters — every tag-sourced dial
black-holes at the router while group/user-sourced dials pass. So participant devices enroll
UNTAGGED (their identity is the person's user) and each team's grant names its people with
`@`-suffixed usernames (bare usernames parse as host references in the v2 policy). Port
granularity needs the `grants` form: one line for the /24 (`ip: ["*"]`), one for the scoreboard
(`ip: ["80"]`).

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
IP* instead; the satellite jump (which owns `.1` there) re-SNATs to the gateway with its
existing engine-source rule. Rules are re-asserted every 30s by a per-deploy
`range-remote-access.timer` (Docker wipes iptables on restart), mirroring `range-firewall`.
The engine template is untouched — no template hash churn.

Isolation stays triple-layered: the headscale ACL (enforced on the engine's tailscale), the
engine's `192.168/16 → 192.168/16` FORWARD DROP, and the portless team bridges.

## What a deploy does (phase 3, `remote_access_ops.setup_remote_access`)

1. Preflight (already done): `TEZ_HEADSCALE_*` present, headscale reachable, **no other live
   router advertising an overlapping `/24`** (concurrent comps with colliding identifiers would
   cross-route each other's participants — that is a hard refusal; use disjoint
   `TF_VAR_team_identifiers`).
2. Install tailscale on the engine (first run), enroll/update it as `eng-<comp-id>` advertising
   every team subnet (routes auto-approve via `autoApprovers` + `tag:range-router`).
3. Push `range-remote-access.sh` + timer (local teams → SNAT `.1`; satellite teams → SNAT
   engine mgmt IP).
4. Create `comp-<id>-<person>` users + single-use preauth keys for everyone in `people.json`;
   re-apply the whole ACL policy from the headscale host's
   `tezcatlipoca-remote-access.json` state file; restart the headscale container (seconds; the
   WireGuard data plane rides through).
5. Write `competitions/<id>/remote-access/` — `enrollment.md` (per-person
   `tailscale up --login-server … --authkey …` commands) and `keys.json` (gitignored, 0600).

Teardown (`destroy-competition.py`) deletes the engine's headscale node (revoking its key),
deletes each participant's devices first (headscale refuses `users destroy` while a user still
owns nodes: "user not empty"), then the comp's participant users, and re-applies the policy
without this comp — warn-and-proceed like the rest of teardown.

## Running it

- **Opt out** per comp: `remote_access 0` in the Compfile (practice/capacity runs that must not
  touch the production tailnet).
- **Roster**: absent `people.json` = no participant grants; the router still enrolls and
  pre-existing users keep full access to the range.
- **Verify**: `verify-competition.py` runs the `remote_access` gate (engine enrolled, subnets
  SERVING, SNAT lines present, policy scoped). Proving what a participant can/cannot reach
  needs a real enrolled device — enroll one from `enrollment.md`, then check:
  own-team SSH works, other team's `/24` times out, `10.0.0.0/24` unreachable except the
  scoreboard `:80`.
- **Full access membership**: edit `full_access` in
  `/home/sysadmin/headscale/config/tezcatlipoca-remote-access.json` on the headscale host (the
  next deploy/teardown re-applies it). New LDAP users must be added there until the
  limiting-by-groups work lands.

## Headscale CLI quick reference (on the VPS; docker is root-only)

```bash
sudo docker exec headscale headscale users list
sudo docker exec headscale headscale nodes list
sudo docker exec headscale headscale nodes list-routes   # note: no `routes` command in 0.26+
sudo docker exec headscale headscale policy get
sudo docker exec headscale headscale policy check --file /etc/headscale/acl.hujson
sudo docker exec headscale headscale preauthkeys create -u <id> --tags tag:range-router \
    --expiration 12h -o json
sudo docker exec headscale headscale nodes delete -i <id> --force
sudo docker exec headscale headscale users destroy -n <name> --force
```

Rollback of the whole policy foundation: `config.yaml.bak-tezcatlipoca-20261008` and
`acl.hujson` live in `/home/sysadmin/headscale/config/` on the VPS; removing the
`policy.path` line + restart returns the tailnet to policy-less allow-all.
