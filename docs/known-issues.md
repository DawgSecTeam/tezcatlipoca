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

### `verify` passes with a scored service down — PARTIALLY FIXED (A3)

Every service verdict was reported `[informational]`, so a range whose SIEM ingest port is closed
still ended `RESULT: PASS`. Live: `team1: 6/7 services UP, DOWN: splunk01-ingest` beside a PASS.
FIXED for the path that matters: scrim's authoritative pre-T0 verify now runs `--strict-services`
and refuses the event on ANY gating FAIL (not just red), so a scored service down can no longer
ride into T0 behind warnings — `--allow-gate-fail <label>` is the only escape, and it is logged.
Still informational on a bare `verify-competition.py` (no `--strict-services`) by design: an
operator probing a half-built range wants the full picture, not a red gate. Unscored services
remain informational in both paths.

### Red loop ran before T0 on a `--hold-start` range — FIXED (A4)

A held deploy started the tlaloc loop at phase-7 end, so red attacked a range the operator was
still provisioning — it stripped freshly-installed forwarders 4.5h before T0 (run-14ac7f14).
Now `--hold-start` leaves the loop enabled-but-stopped with `red_loop_held` in state; only
`competition-ops.py start` (or scrim's T0) releases it. `telemetry.ensure.sh` runs between
`stop_red_director` and the gates so forwarder wiring is red-free and the gate measures the result.

### Zero injects reached the engine — FIXED (A2)

A hold-start deferred inject creation to `competition-ops.py start`, a door the scrim flow never
walked: run-bd70a985 played 120min with zero injects and only a warning. `reanchor_injects` now
creates any missing title against the held jar instead of warning.


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

### gcp provider: the v1 matrix gaps (the Linux range is the cut)

The gcp backend deploys the Linux stack end to end (engine, goldens, boxes, red01, teardown).
Refused at preflight by NAME until each lands — never a silent half-deploy:

- **Windows + AD boxes** — the boot-time channel moves from the QEMU guest agent to
  `startup-script-ps1` on stock GCE images, and the DC boxes' unbooted-golden semantics map to
  reusing the stock image; both are unproven. Refusal: _"Windows boxes need the gcp boot-time
  channel"_.
- **In-path firewalls** — pfSense cannot run on GCP at all; VyOS needs an imported appliance
  image tier, not the PVE clone path. Refusal names the box.
- **unmanaged appliance boxes** — same tier, no gcp path.

Known-open beyond the matrix (these greet you with a clear refusal at use time):

- **redeploy's rollback modes** — a machine image restores by RECREATING the instance; the redeploy
  engine still assumes in-place rollback. `--mode reconfigure` is the only mode wired for gcp.
- **scrim harness on gcp** — `run-agent-scrim.py` does not forward the platform to its internal
  deploy; the dashboard's `platform` param covers deploy/plan only. red01's management channel
  (public-IP SSH, firewalled) works; the harness just needs the pass-through.
- **portal consoles on gcp** — there is no VNC on GCP, so the portal's vncproxy tier has no gcp
  path. A browser-SSH console gateway is the pending design; `portal 1` ranges deploy on proxmox
  only for now.
- **restore points reboot** — a machine image requires a stopped instance, so taking `tz-base`/
  `tz-ready` reboots the box (disk-equivalent restore state, which is what redeploy restores
  anyway; documented in the architecture map).

Also pending: the dashboard's web UI has no platform toggle yet — `platform` rides the MCP tool
and the HTTP API's `params` dict.

### gcp: instances.insert refuses the static regional-address attach on this project

Every reference shape of a real, RESERVED, PREMIUM external IPv4 address — the
www/compute-host full URLs the API itself returns, the relative form (which `subnetwork`
accepts), and the bare name — is refused by `instances.insert` with "The specified external IP
address … was not found in region 'us-central1'", against addresses the same project's list
confirms exist. Probed live 2026-11-06 with both a range reservation and a fresh short-named
one, full-URL subnets alongside (probe instances deleted; the reservations cleaned). Ephemeral
external IPv4 works on every instance unaided. The deploy therefore attaches the engine's
mgmt address as EPHEMERAL at create and records the realized IP in the state document — a
range never outlives its own instances, and a resume reuses the same engine. Revisit a static
pin only if the API starts accepting its own reservations.
