# Known issues

Temporary writepad, not a ledger: every entry has a concrete next action and is drained each run cycle.

## Open

### An orphan reclaim leaves the range reading `live` on the dashboard board

`destroy-competition.py --orphan-comp/--orphan-run` reclaims a range whose workspace is gone by
the VMs' own tags, but nothing tells the dashboard: `range_state` derives `retired` from the
newest `destroy`/`scrim` run with `returncode == 0` in its own store
(`dashboard/ranges.py:retired_by`), and an orphan reclaim is not a run. `has_vms` is evaluated
first and reads the workspace's recorded `resources` — which the failed build left behind and
only a teardown's `forget` clears (`estate/destroy.py`) — so the board keeps showing `live` for
a range that no longer exists. Live 2026-10-15: crimson-relay `range-b289904cf2d5ccdf`
(`deploy_run_id run-b47cd966`) read `live` with `has_vms` true and `resources` naming
engine-template 2340 and bridges vmbr180/181, while an independent Proxmox sweep showed zero
tagged VMs and only `vmbr0`/`untrustedbr` on the node. The same stale record is why a bare
range action for that competition resolves to that dead workspace instead of the newest one
(see the resolver entry below).
**Next action**: give the orphan path a way to retire the range in the store — a dashboard
endpoint the reclaim calls, or have `range_state` re-probe liveness (the tags it already knows)
rather than trusting a workspace whose build run ended non-zero.


### A retired range's kept templates cannot be reclaimed until the competition deploys again

`--teams-only` retires the range and keeps this competition's golden + engine templates for
its next run, which is the documented practice-run loop. But the retired range then blocks
the only path that can destroy them: `_range_workspace` answers a follow-up destroy with
`the range in <run> was torn down by <destroy run> — nothing to destroy`, and a full teardown
run resolves to that retired workspace and dies. Live 2026-10-15 (campaign): pfsense-rvb's
teams-only teardown left engine-template 2540 + goldens 2550-2552, the follow-up full
teardown failed, and the templates were only reclaimed by deploying the competition again
(`run-b7981f23`, which correctly adopted all four) and then tearing *that* range down fully.
The templates are the whole point of teams-only, so this is the normal path, not an edge case.
**Next action**: let a full teardown act on a retired range's recorded template inventory when
that range has no live VMs left, or have the teams-only path record the kept templates
somewhere a later full teardown can find without a deploy.

### The dashboard's range-workspace resolver can pick a stale workspace

`_range_workspace` returns the newest run dir of that competition carrying **any** `run_id`,
so a run that died before it built a range still wins over the workspace holding the live one.
Live 2026-10-15 (campaign): a full pfsense-rvb teardown resolved to an abandoned workspace
(`run-70b9197f/comp`) and failed rc=1, while the live estate was in `run-cac3644a/comp`; only
an explicit `params.comp_dir` reached it. The same shape was behind a subagent briefing
pointing at the wrong workspace for crimson-relay. It also makes the auto-resolved target
depend on submission order rather than on which workspace owns the VMs.
**Next action**: rank candidates by whether their state document records a deployed endpoint /
inventory (i.e. a range that exists), not by run-dir recency; refuse when the winner has no
resources and another candidate does, naming both.

### Two concurrent runs of one competition cannot be given distinct red01 slots

`red01_vmid`/`red01_ip` resolve from the **competition** directory
(`red/badauto_config_ops.red01_vmid_for`/`red01_ip_for`, called at `infra/plan.py:100-101`
and again at phase 7 `red/red_plant_ops.py:247`), while everything else about placement is
per run, and `create-competition.py` has no `--red-vmid`/`--red-ip` (`deploy_lib/cli.py`).
So two concurrent runs of ONE competition read the same reservation: the second is refused
by `infra/red01.py`'s gates, or — if both pass the point-in-time check in the same window —
its bad-auto deploy reconfigures the first run's live red01 (bad-auto's deploy is
clone-or-reuse; that is the 2026-10-12 kill shape the gates were added for). `AGENTS.md`'s
"Concurrent ranges" recipe names only `--scoring-vmid` and team identifiers, so it does not
cover this, and the only lock in play is keyed on the engine vmid
(`nakonops/nakon_lock_ops.py`) — nothing reserves red01.
Measured cost this cycle: the three-parallel-range campaign hand-derived a red01 vmid/IP per
competition and committed a checked-free pair into two Compfiles (competitions repo
`cf68857`) — exactly the "operator must remember it" class this project files as a bug.
**Next action**: carry red01 in the per-run placement object (derived vmid from the slot
count rather than the fixed `+170` offset, address selected from a declared management band)
and write it before preflight, keeping the Compfile keys readable for already-deployed
ranges. Design, blast radius and migration cost: `logs/campaign/placement-simplification.md`.

### The drivers' config contract moved to `TEZ_*` but the deployed env files did not

`estate/config.py` resolves only `TEZ_*` (its `_SPEC` + `env_name`) and no module maps the
legacy `TF_VAR_*` names any more, but the ops host's engine env still defined **only**
`TF_VAR_*`. A deploy survives that — nodes.json supplies the endpoint — so the gap stays
invisible until teardown, whose `verify_clean` needs the ambient config: two teardowns
`destroyed 0` and exited non-zero with `config 'proxmox.endpoint' is not set`, while the
line immediately before said `[estate] ok — destroyed 0`. Same class: bad-auto's
`badauto.deploy.proxmox.load_proxmox_env()` still demands `TF_VAR_proxmox_{endpoint,
api_token,node}`, so a scrim dies deploying red01 over names the pipeline no longer sets;
`dashboard/config.py:engine_config_file`'s docstring still promises the legacy resolution.
**Next action**: have each driver refuse at startup — before any phase, naming the key and
the file that should carry it — when neither `TEZ_PROXMOX_ENDPOINT` nor
`TEZ_PROXMOX_API_TOKEN` resolves, and give bad-auto a `TEZ_PROXMOX_*` read with a `TF_VAR_*`
fallback instead of keeping both name sets alive in an ops-host file by hand.

### A blue cycle can overrun the orchestrator's own event deadline

`run-59a685c0` (2026-10-15, scrim-one-light): a 30-minute `--duration-min` event was still in
`stage=event` at 56 minutes elapsed. `monitor_loop` stopped writing snapshots at T+30 while
the harness waited on a blue cycle that had already exceeded its 1800 s budget, so the event
did not end at its deadline and the clock-bounded deliverables (interaction report, teardown)
were deferred with it. The cycle contract is 6 wall-clock minutes; a model that spends 20+ in
one cycle (one trivial completion took 11.5 min on the local endpoint) turns that into an
unbounded event.
**Next action**: make the deadline authoritative for the feed loop — stop starting cycles past
`duration_min`, and cap the join on an in-flight cycle by the remaining window — so the
harness always reaches capture and teardown on time.

### The comp ownership tag is the workspace directory name, so every dashboard range is `comp-comp`

`teardown/machinery.py` builds `Ownership(Path(comp_dir).name, run_id)` and the deploy stamps
`ctx.comp_name = comp_dir.name` (`deploy_lib/inputs.py`), but the dashboard hands every run a
workspace whose comp dir is literally `<run_dir>/comp`. Every dashboard-deployed range therefore
carries the tag `comp-comp`, and bad-auto's red01 identity marker — also `Path(comp_dir).name` —
reads `comp=comp` on every dashboard red01. Destruction stays scoped (the full ownership set still
requires this deploy's `run-<id>`, and bad-auto's marker also compares its `instance_id`), so this
is attribution, not a false destroy — but `verify_clean` reports every other dashboard range's VMs
as "same competition, DIFFERENT run" (live 2026-10-15: run-44a106d2, tearing down tiny-linux,
listed scrim-one's whole range that way), and the `comp_name` half of the red01 reclaim guard
cannot tell two dashboard competitions apart — only its companions (the no-red skip gate,
bad-auto's instance id) prevented the 2026-10-15 kill. The deploy's template-keep branch reads the
same tag: `infra/collisions.py` compares `comp-<workspace dir name>`, so with the dashboard's
`<run>/comp` workspace a scrim-one-light deploy reported ANOTHER competition's kept golden as its
own ("golden vmid (slot 0) #2251 holds this competition's template — kept for reuse") and phase 4
retagged and reclaimed it before rebuilding the box it actually needed (run-a87e0d88, 2026-10-15).
The hash gate kept the RANGE correct, so this is still attribution rather than a wrong build — but
the identity claim is false and another competition's validated template was destroyed by it.
**Next action**: derive the comp tag from the competition identity the Compfile already carries
(`name`), not from the directory. Changing the derivation alone makes every already-deployed range's
teardown refuse, so it lands as a pre-release identity break — a range still carrying the old tag is
torn down by the code version that wrote it — not behind a compatibility shim.

### `proxmox.mgmt_bridge` is declared and never read, so only a `vmbr0` estate can host a range

The estate profile carries the knob (`estate/config.py`'s spec, and `mgmt_bridge = "vmbr0"` in
`tezcatlipoca.example.toml`) and nothing resolves it: `estate/apply.py:create_engine` builds
`bridges = ["vmbr0"] + …`, `engine/engine_template_ops.py` sets `"net0": "virtio,bridge=vmbr0"`, and
`net/jump_ops.py` its own `{"net0": "virtio,bridge=vmbr0"}`. An estate whose management bridge is
not `vmbr0` (the cyber range's is `vmbr100`) therefore still cannot host a range, and the failure is
a VM attached to a bridge the node may not have rather than a refusal. The per-node half — a
`mgmt_bridge` field on a `nodes.json` record, preferred over the profile, with a preflight that
refuses a bridge the node does not own — is unimplemented.
**Next action**: thread the resolved bridge through `create_engine` (`estate/apply.py`),
`engine/engine_template_ops.py` and `net/jump_ops.py`, read it in the plan, and add
`infra/collisions.check_mgmt_bridge` to the preflight. Needs the user's call first: `create_engine`
is hashed as code-class drift (`estate.apply:create_engine`), so the port makes every existing engine
template report stale and rebuild on its next deploy.

### The template builders reach their scratch VM through node shell

`tools/build-{pfsense,vyos}-provision-template.py` alias `192.168.1.2` on the node's bridge and `ssh -W` through `root@<node>` to drive the scratch VM's bootstrap LAN (`192.168.1.0/24`); their ISO fetch (`download-url`) and template rename (`PUT .../config`) are API-only, leaving this hop and template sync ([multi-node.md](multi-node.md#template-copies)) as the repo's only node-shell uses. The pfSense agent retrofit cannot simply switch to the agent channel instead: installing that agent is what the hop is for. **Next action**: on a host whose shell is closed, either alias `192.168.1.2` on the *operator's* NIC where the operator shares the scratch VM's bridge L2, or console-type an operator-routable bootstrap address (`/monitor sendkey`, which these tools already drive the console with).

### Planted Linux boxes deny all SSH at the PAM account stage after a restart

Root cause unsolved. A stop/start on a planted box can kill SSH preauth (`Access denied ... by PAM account configuration`) while services still listen. `redeploy --mode reset` recognizes the signature and escalates past it. Unreproduced since 2026-09-29 (four stop/starts came back clean); the standing repro lab was destroyed. **Next action**: commit the diagnostic kit (`tools/pam-bisect-lab.py`) so the next occurrence can be bisected.

### Scoring round loop does not auto-resume after an engine reboot

No unattended healing. The shared classifier now distinguishes a stale post-reboot pause from a deliberate one; `verify-competition.py --fix-round-loop` is the repair. **Next action**: a live reboot trial to validate the code path on a real engine.

### Fedora goldens cannot be built on SELinux-enforcing nodes

Mitigated, not closed. The qemu-guest-agent domain is confined on Fedora 44 (dnf/rpm/systemctl denied, `setenforce 0` denied); SSH is the escape hatch (cloud user lands `unconfined_t` with passwordless sudo). The template recipe now sets `SELINUX=permissive` before sealing, but only on rebuild — current node templates still enforce. **Next action**: rebuild the Fedora templates. Keep Fedora out of agent-only lineups until then.

### Student portal: unproven paths from the first live run

2026-10-08 proved console access, the gate, and verify PASS. Still unproven: the Cloudflare tunnel path (no token used), `pveproxy` under event-size concurrency (only one console was open), box rebuilds (console ACL survival), multi-node, teardown revocation (range was left up). **Next action**: a run exercising each.

### Quotient "one session per account" not reproduced on 2026-10-08

Several modules assume a second login evicts the first cookie (the `scoring` second-admin account, a comment in `deploy_lib/context.py`, `usage-people.md`). On `portal-live-2026-10-08`'s engine, two logins as `team1` and separately as `admin` both left the first cookie valid. **Next action**: re-test before relying on the rule either way; the `scoring` account and the student portal are harmless regardless.

### bad-auto `world.json` has no concurrent-writer protection

Upstream-worthy (bad-auto). `World.save()` is last-writer-wins; the director's periodic save can clobber an operator-side `badauto seed` that ran in between (live-found 2026-10-11: a 24+ artifact re-seed vanished under the director's next health-check save, though the planted units stayed live). **Next action**: hand off to bad-auto — needs a save-side reload-merge, not just a lock.

### bad-auto clones red01 before it validates its SSH key

Upstream-worthy (bad-auto). `badauto deploy` clones the VM and only then reads `<deploy.ssh_key>.pub`, so an unresolvable key leaves a cloned, unstamped, unstarted red01 holding the LLM key — the shape live-found 2026-10-14, when a red-staging run whose bad-auto config carried no `deploy.ssh_key` fell back to bad-auto's built-in default (a sibling checkout's `proxmox` that no run host has). The engine side is closed: `core.ssh_key_path()` pins the resolved absolute key on every bad-auto config both writers produce, and an unstamped red01 is reclaimed on both teardown paths (`red: pin the RANGE's ssh key on every bad-auto config it writes`, `red01: one narrow reclaim for an unstamped red01`). **Next action**: hand off to bad-auto — read and validate the public half before the clone, so no caller can leave a VM behind.

### `verify_clean` proves red01 gone by bare vmid membership

`teardown/engine.py` and `scrim/teardown_stage.py` both answer "is red01 gone?" with `int(vmid) in live_vmids()` — every vmid the endpoint's cluster view knows, with no tag, comp or node filter — so an occupant at that vmid cannot be attributed to this range or to another. The verdict stays fail-closed (an unaccounted-for vmid is never clean), so this is attribution robustness, not a false refusal: the 2026-10-14 refusals were correct. The marker reader the deploy preflight already uses (`infra/red01.py`, bad-auto's `bad-auto red01 comp=<name> id=<uuid>` description) is the pattern to follow. **Next action**: attribute the occupant and keep failing closed when it carries no marker; validating it needs a live vmid collision.

### scrim-one's verify gate is red on two checks in every deploy

`pins_registered` ("12 pinned checks") and `misconfig` ("misconfig did not survive cloning
everywhere") FAIL while the other ten gates PASS, identically on two independent deploys
(2026-10-14: run-df29374d and run-e7e12115 — `run.log` verify block, `RESULT: FAIL — see failing
checks above`). The range deploys, scores and heals, so this is content drift, not plumbing: either
the golden-set/seed order drops the pins and the misconfigs before the clones are taken, or both
checks look somewhere the clones do not have. **Next action**: run the two checks by hand against
one clone and against its golden, then fix whichever of planting order / check path is wrong; both
gates must be green before scrim-one is used for scoring.

### A blue agent never produces its narrative

`BLUE-TEAM.md` is a 524-byte stub, and the harness logged `BLUE-TEAM.md is ABSENT although this run
had a blue agent` at finalize (2026-10-14, run-e7e12115; same shape as the run15 finding "Blue never
writes REPORT.md when its cycles time out → BLUE-TEAM.md is a stub every run"). Blue's side of the
report therefore has to be reconstructed from `INTERACTION.md` and the cycle logs every time.
**Next action**: make the blue side write its narrative at the end of the event whatever its cycles
did (including the timeout path), and keep the collector's "present agent, missing canonical
document" warning as the guard.

### A 45-minute event holds two blue cycles at current endpoint latency

Measured 2026-10-14 (run-e7e12115): a blue cycle costs 20–30 min wall clock (LLM decision ~220 s at
`qwen3.8-27b` on `10.0.0.143:8080`), the two teams serialise on a shared LLM lock, and the event
therefore produced 4 cycles total — below the rehearsal gates that require `>= 6` cycles and
`>= 2` restorations, so "NOT READY" is partly an artefact of the clock rather than of the teams'
behaviour. **Next action**: decide deliberately — recalibrate the blue gates to the endpoint the
club actually has, or change the budget (longer event, faster model, or per-team endpoints instead
of one lock) — and record the decision next to the gate table in `docs/rehearsal-gates.md`.

### `misconfig` is red in every verify, deploy-time and pre-T0 (scoring integrity)

Live 2026-10-15 (run-1c44733b, scrim-one, 2 teams): `misconfig : FAIL  misconfig did not survive
cloning everywhere` at the deploy-time verify AND at the pre-T0 re-verify, while `plant_coverage`
PASSed all 12 machines and every other gate came back green. Two independent runs have now shown
the same shape, so this is not flake. A third run names the config: run-a87e0d88 (scrim-one-light,
2 teams) reports `FAIL  'bad-perms-userConfig' absent on every team (['web01-team152',
'web01-team153']) — the config didn't plant on any clone` while `'writable-sudoers' present on all
2 team(s)` — so one expected misconfig never reaches the clones at all, and it is not every
misconfig. **Next action**: root-cause it in the gate, with a regression test — a comp whose pins
are provably planted on the goldens and confirmed on the clones must pass. This is the one deploy
gate whose failure means the scored range is not what the packet promises.

### The tailnet→team SNAT rule flaps, and a container restart is what drops it

Live 2026-10-15 (run-9fd68d14, scrim-one-light canary): `remote_access : FAIL  SNAT lines missing
from the engine nat table` on a range whose identical probe PASSed minutes before and again
after. Sampling the engine's nat table every 10 s showed the rule present for nine samples, absent
for three, present again. The engine journal names the cause: `tailscaled: LinkChange: major,
rebinding: ... diff: ips divisor: [...] rebind-reason=[ips-changed]` — the Quotient container's
interface gaining an address makes tailscaled re-apply its ruleset, which takes the POSTROUTING
chain with it; `range-remote-access.timer` only re-adds the rule every 30 s, so tailnet→team
traffic is genuinely broken inside each window. The verifier now samples past one re-assert cycle
(`e4e17a6`), so the gate no longer calls a self-healing range dead, but the flap itself is open.
**Next action**: re-assert on the rebind instead of on a timer (a unit triggered by the interface
change, or `tailscale set` completion) or move the rules where tailscaled's ruleset cannot reach
them, then prove it by holding a tailnet→team flow across a container restart.

### red01's liveness is probed on the same path its traffic uses, so a node network bounce loses red silently

Live 2026-10-15 (run-1c44733b): red01 (10.0.0.198) went unreachable at T+13 — every 60 s probe
returned `No route to host` for the remaining 30 minutes, the in-run snapshots stopped at
`events-T+013.jsonl`, and the teardown could not collect `RED-TEAM.md`. Red had 13 minutes of the
45-minute event; six red gates failed as a direct consequence. The harness did everything right
(the alert fired at the third miss, and the absence was recorded rather than stubbed), but nothing
can distinguish "red is dead" from "the path to red is dead" — both are the same probe. A second
session's range churn on the same node (repeat deploy/destroy, each creating and destroying bridges
and running `ifreload -a`) brackets the exact minute, and is the leading explanation.
**Next action**: give a live event's red01 an out-of-band liveness signal (a second address family,
a guest-agent ping, or a heartbeat the box itself pushes to the operator) and make the run log say
which one answered; run at most one range per node while an event is live.

### The scoreboard inject detail page renders markdown literally and prints the title twice

Upstream `dbaseqp/Quotient` bug in `static/templates/pages/injects.html`: the detail pane sets
`descPara.textContent = inject.Description`, so every inject's markdown description (`#`, `##`,
`**bold**`, lists) shows as one unformatted paragraph, and both `pane__from` and `pane__subject`
get `inject.Title`, so the title prints twice. The `marked` library is already loaded on every
page and `red.html` already renders markdown with it — the fix is two lines. Applied in place on
2026-10-15 to the only live engine (patched `/opt/quotient/static/templates/pages/injects.html`
and restarted `quotient_server`; the container mounts `./static` from the checkout and templates
are parsed at startup, so no rebuild is involved). The canonical copy is local Quotient main
`ed539df` — unpushable, this host lacks write access to `dbaseqp/Quotient`.
**Next action**: get the two-line fix upstream (push access to `dbaseqp/Quotient`, or pin a fork
via `quotient_ref`), because every engine bootstrap re-clones stock upstream and re-introduces
the bug; re-patch the live engine whenever a new one deploys in the meantime.

### A deleted per-run workspace strands the range that run deployed

The dashboard removes each run's workspace when the run ends — including a run that FAILED and a
destroy that ended `teardown_failed`. Verified 2026-10-15: of 51 range records, 29 were `failed` and
4 `teardown_failed`, and all four `teardown_failed` workspaces (`run-48c4d760`, `run-adb8e61a`,
`run-27b0d9e4`, `run-c84faaac`) had no `comp/estate-state.json` left. Teardown is journaled and
idempotent precisely so a failed destroy can be re-run, but the journal, the run id and the ownership
set all live in that workspace, so deleting it removes the sanctioned way to finish. The leftover VMs
are then reachable only by their own tags (`destroy-competition.py --orphan-comp/--orphan-run`, added
this cycle), which cannot resume the plan, cannot tell a deliberately kept template from debris, and
cannot consult the freeze record. Live 2026-10-15: .150 carried two engine-templates and seven goldens
under two run ids with no workspace anywhere, and the dashboard's range view had gone stale — one
`live` record still named vmid 2101 after that VM was gone; the API tags were the only truth.
**Next action**: hold the workspace of any run whose range is not `retired`, or have the dashboard
submit an orphan reclaim when it deletes one.

### bad-auto's assume-breach seed exits 0 with every step failed, and the scrim believes it

scrim-one-light run-1ea5494a-run2 (2026-10-15, red+blue scrim of a deployed range): the
authoritative pre-T0 verify printed `red_teams : PASS  red01 reached all 2 teams`, and **seven
seconds later** bad-auto's seed reported all six boxes `"step": "access", "detail":
"192.168.<tid>.<n> unreachable over SSH"` — then exited **0**. `scrim/red_stage.seed_assume_breach`
gates on that exit code alone, so it logged "assume-breach presence seeded — every box is
compromised before T0" and started the clock. Red then spent the whole 30 minutes with **0
footholds** (every red event reads `0 footholds alive, 0 evicted, 0 artifacts standing`), reached
for one tactic (`db_attack` mysql recon, 10 ok / 2 failed actions) and scored **0 takedowns**; blue
ran 3 cycles, logged 4 evictions and 2 injects and had nothing to restore. Interaction score 11,
9 of 12 rehearsal gates failed. Red's LLM calls also timed out on the local endpoint three times
(`no answer in 300s ... localhost:8180`) and it stalled T+8 → T+21 — the endpoint is too slow for
red's 300 s decision budget even when the seed works.
The two observations cannot both hold: either the verifier's `red_teams` probe and bad-auto's seed
use different transports/credentials/target lists, or the seed evaluates a path the gate does not.
That is a bad-auto (tlaloc) contract question — this repo can only see bad-auto's stdout.
**Next action**: make bad-auto's `seed` exit non-zero when no step succeeded (or emit a
machine-readable per-step result the harness can gate on), then have `seed_assume_breach` refuse
T0 on a seed that achieved nothing instead of announcing success; the current guard is faithful to
its intent but blind to the only signal that matters. Needs bad-auto's owner — it is a sibling
repo's exit-code contract, not a tezcatlipoca change.

## Pending upstream — catalog / nakon

- **`IIS FTP` golden plant is timing-fragile and non-idempotent** (catalog DB step body). `Start-Service FTPSVC`/`New-WebFtpSite` can race the just-installed config store; the step's `$Error.Count -gt 0` footer turns a transient into a hard failure, and the body doesn't re-run cleanly without the phase-4 `tz-base` rollback. Recovery: `--from-phase 4 --yes`. Failed-run logs are preserved on the engine (`/opt/nakon/failed-<tag>-<ts>`) since 2026-10-11.
- **`mailenable-cleartext-mail-win`** — the only remaining `KNOWN_BROKEN_CONFIGS` row. `choco install mailenable` names a package the community feed doesn't have, so only the firewall rule lands; MailEnable's real installer is the likely fix.
- **`nakon randomize` can hand out names whose script needs vars it can't supply** — catalog half fixed (`catalog check` reports `missing-vars`; the driver filters unplantable bare names). `randomize` itself is unchanged, so `catalog check` remains the gate.
- **Windows package-manager fallback** — writes `.nakon-step-rc` and states plainly it has never run against real winget/chocolatey. Still not live-verified.

`constants.KNOWN_BROKEN_CONFIGS` is the repo-side guard, enforced at generate time and packet-compile time: a fresh selection hitting a broken row is a hard error; a competition that already records the pin gets a warning instead (so historical comps stay re-deployable). Delete a row here when fixed upstream — don't archive it.

## Standing limitations — not fixable here

- **Windows scoring is port-open only.** Every Windows service check maps to the generic `Tcp` slice (WinRM 5985, SMB 445, RDP 3389); it can't distinguish a healthy service from one merely listening.
- **Nakon step failures are recorded but never fatal by design.** A failed install shows as `FAILED` in `nakon_failed_steps`, surfaced by verify as a plant-integrity line; the deploy never raises on it.
- **Orphaned ranges and vmid contention on shared nodes.** Not a code bug — discarded comp dirs leave VMs holding vmids/disk/golden slots. Reclaim with `destroy-competition.py` from the matching comp dir, never an ad-hoc sweep.
