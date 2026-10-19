# Remote access — participants reach their own team network over headscale

Every deploy enrolls the scoring engine as a **headscale subnet router** advertising every team
`/24`, and hands each participant a key that reaches **only their own team's subnet** plus the
scoreboard. `net/remote_access_ops.py` is the only writer of the ACL policy; `render_policy`
rebuilds it whole on every deploy and teardown that touches remote access. Pre-existing tailnet
users (the LDAP-backed accounts) keep tailnet-wide access through `group:full-access`.

Control plane: the estate's headscale instance (URL, host and credentials:
`infrastructure-status.md`). Version and CLI quirks are
[environment-facts.md](environment-facts.md#headscale) — not repeated here.

**One router advertises ALL team subnets** — per-team router VMs are not needed: tailscaled on the
engine enforces the ACL per destination subnet before a packet ever reaches a team bridge.

**Why usernames, not tags.** Headscale never expands tag *sources* into the routers' packet
filters, so every tag-sourced dial black-holes at the router even though the node's own netmap
routes look right. Participant devices therefore enroll UNTAGGED (a single-use preauth key per
person, TTL `headscale.key_ttl`; users `comp-<id>-<person>`, engine node `eng-<comp-id>`
`tag:range-router`, roster `people.json` in the comp dir) and each team's grant names its people's
`@`-suffixed usernames.

Participant traffic is SNATed to the team gateway `192.168.<id>.1` on the engine
(`remote_access_firewall_script`) — box sshd accepts gateway-IP auth only, so traffic must arrive
as the gateway. **Satellite teams** SNAT to the engine's *mgmt IP* instead; the satellite jump
(which owns `.1` there) re-SNATs to the gateway with its existing engine-source rule.

tailscaled re-applies its ruleset whenever an interface gains an address and takes the POSTROUTING
chain with it (Docker also wipes iptables on restart), so per-deploy `range-remote-access*`
services on the engine watch for the event and re-assert, with a timer as fallback. The engine
template is untouched — no template hash churn. Isolation stays triple-layered: the headscale ACL,
the engine's `192.168/16 → 192.168/16` FORWARD DROP, and the portless team bridges.

## Gate and refusal semantics

`infra/gates.py::remote_access_gate` requires the `TEZ_HEADSCALE_*` config present and the control
plane reachable — remote access ships with every deploy, so a dead control plane must stop the
deploy at the gate, not mid-phase-3. The deploy then **refuses** a live router elsewhere
advertising an overlapping `/24`: two routers on one `/24` are one HA route group and would
cross-route each other's participants. Use disjoint `TEZ_TEAM_IDENTIFIERS` or tear the other range
down. The engine's routes auto-approve via the control plane's `autoApprovers`. Creating users
re-applies the whole ACL policy: the control plane restarts for seconds; the WireGuard data plane
rides through.

Teardown (`destroy-competition.py`) deletes the engine's headscale node (revoking its key), then
**each participant's devices before their users** — headscale refuses `users destroy` while a user
still owns nodes — then the comp's participant users, and re-applies the policy without this comp.
Warn-and-proceed like the rest of teardown.

## Running it

- **Enrollment** lands in `competitions/<id>/remote-access/`: `enrollment.md` (per-person
  `tailscale up --login-server … --authkey …` commands) and `keys.json` (gitignored, 0600). The
  student portal surfaces the same ([portal.md](portal.md)).
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
