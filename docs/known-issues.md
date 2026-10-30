### Student portal: unproven paths

A single-node two-team, four-box acceptance run (`portal-smoke`) proved health/login/scoping, the closed gate (`423`), cross-team refusal (`403`), four truly concurrent relays (each `101` + `RFB 003.008`), ACL removal/reapply across a real box rebuild, and full teardown revocation (the exact `tezcon-<comp>-<engine-run-id>@pve` user and its ACLs gone). A second one-box run (`tiny-portal-smoke`) proved the **gate-open** path: `open: True`, team gate `HTTP 200`, and the console relay. A third one-box run proved the **public Cloudflare path** with a quick tunnel (`trycloudflare.com`): tunnel healthz `200`, tunnel team login `200`, and a tunnel console relay `wss 101` + `RFB 003.008`. Still unproven: the deploy-side **named-tunnel start** (`TEZ_PORTAL_TUNNEL_TOKEN`/`TEZ_PORTAL_HOSTNAME`, including the ownership decision) because the token is absent, and multi-node (the field node was down). **Next action**: run with the event's tunnel token/hostname and a two-node range.

### Placement probe over-scopes the vmid collision check

`multinode/placement_planner.probe_node` refuses a satellite whose existing VMs occupy ANY slot
the placement *might* use (`engine_vmid`, `+140`, `+150..`), even when that node will not host the
engine in the chosen layout. Consequence: a satellite running an unrelated competition's engine
(the same derived vmid band, from a previous worktree) is disqualified from hosting a satellite,
with the collision named as if a real clash existed. The workaround is choosing another node, and
`--scoring-vmid` does not help. **Fix to design**: probe per-role — only require the engine's own
slots on the node the engine will actually run on, and for satellites require only the jump vmid and
their assigned golden band, both derivable once the layout is fixed. Not changed yet because the
probe runs before the layout is chosen and every caller would need the layout passed in.

### `verifier/packet.py` scores only the first team's boxes

The packet-integrity check probes one box per competition (the first Linux box of the first team),
so a box that failed to boot or a service that did not start on any other team passes. Every team's
first Linux box already has a recorded target; widening it is a loop over `team_keys()` instead of a
single pick, but the check's runtime grows with the team count and it currently runs inside the same
timeout as the scoreboard gates.

### A failed deploy's own bridge blocks the retry (placement probe)

The deploy's placement probe counts anything already on the target vmid/bridge as a collision,
including the *same competition's* leftovers from the deploy that just failed. Evidence: after a
phase-3 failure left engine vmid `1500` and `vmbr120` behind, every retry died at
`no eligible node for team(s) team1 … vmid collisions: [1500], bridge collisions: ['vmbr120']`
and the only way forward was to destroy the failed workspace, which throws away the goldens and
engine template that run had already built. The single-node path handles this (phase 1 reclaims,
and `check_collisions` reports such a VM as reclaimable); the multi-node probe does not, and
bridges carry no ownership tags so it cannot tell ours from a stranger's by tag alone. The
engine's mgmt-IP gate had the same blind spot for the engine-template slot and is fixed
(`infra/collisions._own_build_vmids`); the placement probe needs the equivalent — provisionally,
skip a bridge whose name matches one this plan would create when the node's engine VM carries
this competition's tag.

### `verify` passes with a scored service down

Every service verdict is reported `[informational]`, so a range whose SIEM ingest port is closed
still ends `RESULT: PASS — competition looks healthy`. Live: `team1: 6/7 services UP, DOWN:
splunk01-ingest` alongside a PASS summary. A scored service being down should fail the run (or
at least the summary line should not read PASS); the split exists so an unscored service cannot
fail an otherwise healthy range, but it currently also hides the scored ones.

### The queue cannot resume a failed deploy (`--from-phase`)

`--from-phase` is unusable through the dashboard: `deploy` is not a `RANGE_ACTION`, so a
resubmitted deploy seeds a fresh workspace and the driver dies with
`--from-phase 2 but …/run-<id>/comp/estate-state.json doesn't exist`. `redeploy` is not a
substitute — its modes act on a settled range, not on the phases a failed deploy never reached.
Recovering a half-built range therefore means either a full destroy+deploy (rebuilding templates
that were already proven) or a manual driver run in a new worktree. A `resume` run action that
resolves the owning workspace and passes the highest completed phase would cover it.

### The run's stage label lags the driver's real phase

`GET /api/runs/<id>.stage` trails the engine: a run sat at `phase 6/8 repair_sweep` through the
whole red01/Mythic install (real phase 7) and only jumped to `phase 8/8 seed` on completion.
`watch_run` subscribers therefore read a stale phase for the longest single step of a deploy.
