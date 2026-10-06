# Usage — agents

Non-interactive interface: CLI flags, pre-authored config files, and machine-readable
validation, for driving this from a script or an agent instead of answering prompts by hand.
A browser front end wraps the same drivers for authoring and deploying, see
[../webui/README.md](../webui/README.md).
One-time Proxmox/template setup is still a human prerequisite — see
[usage-people.md](usage-people.md#prerequisites-one-time-per-proxmox-host) for that. For what
the project is and how it's architected, see the [README](../README.md).

## `create-competition.py` flags

With no flags it runs fully interactively. Flags let it skip prompts individually — you don't
need all of them, only enough to cover what you'd otherwise be asked:

| Flag | Effect |
|---|---|
| `--competition NAME` | Deploys it straight away if it already has a `Compfile` + `boxes.json`; otherwise creates it (needs `--scenario`/`--difficulty` or falls back to prompting for them). |
| `--teams N` | Number of teams — skips the "How many teams?" prompt. Must be 1–154 (`MAX_TEAMS`: team identifiers are `192.168.<101-254>.x`). |
| `--yes` | Skips the confirm-deploy prompt. |
| `--scenario TEXT` | Scenario description (only used when creating a new competition). |
| `--difficulty N` | Difficulty 1–10 (only used when creating a new competition). |
| `--from-phase N` | Resume from this phase; `N > 1` skips the destructive cleanup + `terraform apply`. Refused when it would skip a phase `.deploy_state.json` does not record as completed (`last_phase`). See the resume hint a failed deploy prints. |
| `--force-from-phase` | Proceed anyway when `--from-phase` skips phases the state file never saw complete. Only for a checkpoint known to be stale (e.g. the process died after a phase finished but before its checkpoint landed) — the skipped phases build the machines the later ones target. |
| `--plan-only` | Collect/generate the competition's config and print a summary, then exit **without touching any infrastructure** — no teardown, no `terraform apply`. |
| `--box-username NAME` | Themeable box login username (only used when creating a new competition; default `ubuntu`). Written to `competitions/<id>/users.json`. |
| `--credlist-usernames A,B,C` | Themeable credlist account names, exactly 3 comma-separated (only used when creating a new competition; default `admin,user1,user2`). Written to the same `users.json`. |

**Box selection has no flag equivalent** — naming/templating/sizing each box type
(`collect_boxes()`) is always interactive, even under `--yes`. To skip it non-interactively,
pre-author `competitions/<id>/Compfile` + `boxes.json` yourself (see
[Pre-authoring a competition](#pre-authoring-a-competition) below) and pass just
`--competition <id>`; the tool detects both files exist and deploys straight away, prompting
only for team count (add `--teams N` to remove that prompt too).

Run `python3 create-competition.py --help` for the exact current list.

### Review before a real deploy

Even with `--yes`, or with piped/scripted stdin that happens to satisfy every remaining prompt,
nothing stops the driver from walking straight into a real `terraform apply`. Use
`--plan-only` first to collect/generate the config and print the box list without touching
infrastructure, review it, then re-run the same command without `--plan-only` (plus
`--teams N --yes`) to actually deploy.

### Resuming (`--from-phase`)

`deploy()` walks the eight **v3** phases (`deploy_lib.phases.PHASES`) — 1 Cleanup · 2 engine-template + apply #1 · 3 prepare
engine from template · 4 golden set + apply #2 · 5 firewall bootstrap (in-path firewalls only) ·
6 repair-stage sweep · 7 domains → final pass → beacons + `tz-ready` · 8 seed.
[architecture.md](architecture.md#eight-phase-deploy) is the
canonical list. A failed deploy prints which phase it died in; `--from-phase N`
re-enters there instead of tearing everything down. **Cloning is phase 4** (linked clones in
apply #2) — phases 6-8 have no clone step and do not read the nakon bundle by hash. Phase 8's three
sub-steps (seed teams, unpause the engine, create injects) are each gated on their own flag in
`competitions/<id>/.deploy_state.json` (`seeded`/`engine_unpaused`/`injects_created`) — a
`--from-phase 7` resume after a partial phase-7 failure skips whatever already succeeded rather
than re-running it, since re-unpausing an already-unpaused engine isn't safe (see
`quotient/setup.py`'s `unpause_engine()` docstring) and re-creating injects would duplicate
them. If phase 7 needs to be forced to redo a step anyway, edit those flags out of
`.deploy_state.json` first.

**The resume target is checked, not just recorded.** Each phase writes `last_phase` after it
completes, and `--from-phase N` is refused when `N` skips a phase that never completed (`N >
last_phase + 1`): the skipped phases are what build the machines the later ones target, so
resuming there would run the domain/final/seed chain against boxes that do not exist and bury the
real failure. A state file with a missing/unusable `last_phase` reads as `0` (the conservative
choice). `--force-from-phase` is the escape hatch for a checkpoint you know is stale — use it
deliberately, not to silence the guard. Resuming a state file written by another pipeline version
is refused; see [state-file schemas](architecture.md#state-files).

## Pre-authoring a competition

To create a competition with zero interactive prompts, write these two files yourself instead
of letting `create-competition.py` generate them:

```
competitions/<id>/Compfile     # name, scenario, difficulty
competitions/<id>/boxes.json   # box list: name/template/cpu/memory_mb/disk_gb/last_octet
competitions/<id>/users.json   # optional — themeable box/credlist usernames, see below
```

See `competitions/example/` for the exact shape. Then:

```bash
python3 create-competition.py --competition <id> --teams N --yes
```

`users.json` is optional (default `ubuntu`/`admin,user1,user2` when absent) —
`{"box_username": "engineer", "credlist_usernames": ["svc-admin", "analyst1", "analyst2"]}`.
When creating a new competition non-interactively, `--box-username`/`--credlist-usernames`
write it for you instead of hand-authoring the file.

## Pinning a competition's configuration

Randomising which services/misconfigs land on each box is the default. To choose the set
yourself instead, write either (or both) of these, keyed by box **type** (not per team — every
team defends the identical set for Quotient's wildcard-IP checks):

```
competitions/<id>/box_services.json   {"web01": ["apache"]}          scoreable services
competitions/<id>/box_vulns.json      {"web01": ["suid-find", ...]}  planted misconfigs
```

Either file on its own is enough to count as pinned; if neither exists, the run randomises
and then writes both, so a re-run of the same competition reproduces it exactly.

Entries can be a plain string **or** an object with vars —
`{"name": "Run/RunOnce Keys", "vars": {"process": "WindowsUpdateHelper", ...}}` — for the
catalog configs that take parameters (see `vendor/nakon/config-example.json`). Windows
domain roles (`ADDS`, `Domain Join`) must never appear here: they reboot the box and are
driven separately by `deploy_domain_configs()` (see `domain_roles.json` in
[usage-people.md](usage-people.md#windows-domain-join-boxes)).

nakon exposes the catalog and a validator for building these programmatically. Run them from
`vendor/nakon/` — that's where the module and its `.env` (catalog creds) live:

```bash
cd vendor/nakon
python3 -m nakon catalog list --json
python3 -m nakon catalog check --box-vulns /abs/path/to/competitions/<id>/box_vulns.json
```

`catalog check` catches typos, building blocks requested directly instead of the misconfig
that wraps them, and platform mismatches before a deploy. See `docs/agent-selection.md` in the
nakon repo for the full catalog format.

## `generate-packet.py`

```bash
python3 generate-packet.py competitions/<id>
```

Renders `competitions/<id>/packet.md` — a single, team-agnostic Markdown briefing (network
layout, per-box services, the configured box login username, inject schedule if any, rules of
engagement) meant to be handed to competitors ahead of the event, before real credentials
exist. Pure local-file read: needs only `Compfile`, `boxes.json`, and `box_services.json` to
exist (any combination of generated-by-`create-competition.py` or hand-authored/pinned per the
sections above) — no live deploy, no Proxmox/Quotient access. Deliberately never reads
`box_vulns.json`, so planted misconfigs never leak into it. Re-run any time those inputs change
to regenerate; it always overwrites `packet.md` in full.

## `verify-competition.py`

Post-deploy smoke test — reproduces the manual checks run by hand after a deploy:

```bash
python3 verify-competition.py competitions/<id> [--engine-ip IP] [--admin-password PW] \
    [--strict-services] [--allow-unverified GATE]... [--timeout SECONDS]
```

1. **LOGIN** — every team account and admin can `POST /api/login` (HTTP 200).
2. **SERVICES** — each team's services report UP in the latest scored round (informational
   unless `--strict-services`, which also requires something to have been scored and the newest
   round to be fresh).
3. **ISOLATION** — the team-to-team DROP rule (`range-firewall.sh`) is present in the engine's
   `FORWARD` chain, and — with 2+ teams — an actual connection from one team's box to
   another's is confirmed blocked **while the target is provably alive and that same box still
   reaches the internet**. Rule presence alone can't tell a correct rule from one that's shadowed
   or misordered, and a stopped box looks identical to a blocked one, so an unprovable probe is
   `SKIP`, not a pass.
4. **MISCONFIG** — at least one planted misconfig is present on a target box (SSH via the
   scoring-engine gateway). A follow-on **misconfig-survival** pass checks every team's copy of
   each verifiable config (2+ teams) is identically present — present on some teams but not
   others FAILs, and absent on **every** team FAILs too (the plant never landed anywhere).
5. **INJECTS** — if the competition ships an `injects/` dir, the engine has that many injects.
6. **PINS** (`pins_registered`) — every pinned service's `<box>-<Display>` check is registered in
   Quotient. Gate is active only when the comp has pinned services.
7. **PLANT COVERAGE** (`plant_coverage`) — reads `.deploy_state.json`'s per-machine
   expected-vs-planted record, falls back to the nakon FAILED tally, and **fails closed**: when
   neither source exists it FAILs ("coverage was never recorded") rather than passing vacuously.
   Phase 4's golden plant records its own verdict (the `{box}-golden` keys, slot-qualified on
   satellites), so a lineup with no post-clone configs is still recorded — golden-stage failures
   (including `alpine_services`-tolerated ones) map onto every team clone.
8. **DOMAINS** (`domains`) — AD promotion, joins, AD plants, and cross-team DomainSID uniqueness
   (see `verifier/domains.py`'s `check_domains`). Fail-closed on a malformed `domain_roles.json`.
9. **RED IDENTITY** (`red_identity`) — only with `--red-identity`: proves red's source address
   survives end-to-end to the boxes (see `--red-ip`/`--red-seg-ip`/`--red-user`).
10. **PACKET** (`packet_creds` + `packet_accounts`) — only with `--packet <profile>`: the packet's
    published credentials match `credentials.txt`, and its `out_of_scope` decoy accounts exist.
11. **ROUND LOOP** (`round_loop`) — the scoring round loop is actually advancing; it does not
    auto-resume after an engine reboot.

**Gate model: PASS / FAIL / SKIP, and a SKIP is not a pass.** Every gate returns a tri-state
result, and the SUMMARY and the exit code are generated from the same results, so they cannot
disagree. `SKIP` means the gate could not be evaluated (dead SSH, missing state, no vantage
point) and is **non-passing** by default — a check that never ran exiting 0 is how a dead box
reads as a healthy range (live-found 2026-10-02: isolation's cross-team probe passed on stopped
VMs). A few results are deliberately *non-gating* and shown as `[informational]` in the SUMMARY:
the structurally-not-applicable cases (no `nakon-config.json`, no `domain_roles.json`, no
`injects/` dir, `--expect-no-vulns`, fewer than 2 teams for misconfig-survival). Waive a gating
gate's SKIP with `--allow-unverified <gate>` (repeatable); it waives only a SKIP,
never a FAIL, and names that match no gate in the run are warned about. `--timeout SECONDS` sets
an optional whole-run wall-clock budget (default `0` = off), checked **between** gates so it never
interrupts a Proxmox task; a gate skipped on budget is recorded `SKIP` and so is non-passing too.
**Operators: a range whose verify used to exit 0 can now exit non-zero for a gate that never
ran** — that is the intended fail-closed behavior; fix the gate or waive it explicitly.

Two diagnostic lines: a `no_default_creds` regression guard (part of the exit-code gate, same as
logins/isolation/misconfig/injects) confirming `credentials.txt`'s box-login/credlist lines aren't
the old fixed literals, and a purely informational report of `range-healthcheck.timer`'s status plus
`report_beacons` and the per-pass `plant integrity` tally from `.deploy_state.json` — none of those
affect the exit code.

Conditional gates added with the in-path firewall and clean comps: **`firewall_in_path`** (comps
with an `in_path` firewall: per team, the engine routes `192.168.<id>.1` via `172.31.<id>.2`, the
firewall answers on `172.31.<id>.2:22`, and the engine no longer holds `192.168.<id>.1` — catches
an out-of-band `terraform apply` re-writing the pre-cutover netplan; SKIP and non-gating with no
firewall, SKIP when the engine is unreachable) and an **automatic misconfig SKIP** for a
deliberately-clean comp (`box_vulns.json` empty and no machine carries a misconfig — informational,
no flag needed; pinned vulns that the machines lack still FAIL).

Exit code is `0` only when logins all pass, no default creds remain, isolation holds, the
misconfig spot-check (and, with 2+ teams, the misconfig-survival pass) confirms, injects
(if any) are present, the round loop is advancing, and the conditional gates above pass — and no
gating gate is an unwaived SKIP. Service DOWN is reported but not fatal unless
`--strict-services`. Isolation is **not** demoted to informational the way services are — a failed
isolation check means teams can reach each other right now.

Other flags: `--expect-no-vulns` skips the misconfig gates for a packet-compiled comp that has not
authored `box_vulns.json` yet; `--fix-round-loop` POSTs the start/unpause pair when the engine's
round loop did not auto-resume after a reboot (the run still FAILs — re-run verify to confirm a
fresh round); `--freeze` (with `--windows-domain-validated` for Windows/domain lineups) writes the
`.frozen.json` record, and `--unfreeze --confirm-unfreeze` removes it.

Data sources (all read at runtime): scoring-engine IP from `terraform output -json`
(override with `--engine-ip`), team creds from `competitions/<id>/teams.json`, admin password
from `competitions/<id>/credentials.txt` (override with `--admin-password`), SSH key/user from
`.env`, planted configs from `competitions/<id>/nakon-config.json`.

## `redeploy-competition.py`

> Module layout (0.2.0): the CLI is unchanged; the code is split into `redeploy_*_ops.py` modules — see [internals.md](internals.md#redeploy-competitionpy).

Puts a **subset** of a live competition's boxes back without tearing down the range — the tool
for "team 3's web01 is wrecked and the event is still running". `create-competition.py` can't do
this: its phase 1 destroys every team, and even `--from-phase` operates on the whole
competition.

```bash
python3 redeploy-competition.py --competition <id> [selection] [--mode M] [--dry-run] [--yes]
```

### Selection (AND-combined; no filters = every box)

| Flag | Effect |
|---|---|
| `--competition ID` | Which competition. Omit for an interactive menu. |
| `--teams 2,team3,104` | Teams, by key (`team2`), number (`2`), or subnet identifier (`102`). |
| `--boxes web01,db01` | Box names from `boxes.json`. |
| `--platform linux\|windows` | Boxes whose template is that platform, via nakon's `os_to_platform()`. |
| `--dry-run` | Print the resolved targets (vm name, vmid, IP, snapshots present) and exit. |
| `--yes` | Skip the confirmation prompt. |

Anything that matches no team/box is a hard error, not a silent empty selection.

### Redeploy modes (cheapest first)

| Mode | What it does | When |
|---|---|---|
| `rollback-ready` *(default)* | Roll back to the `tz-ready` snapshot, restart, wait for SSH. ~seconds per box. | The box was fine at hour zero and isn't now. |
| `rollback-base` | Roll back to `tz-base`, then re-run DNS/auth/`nakon deploy --only`/hardening and re-take `tz-ready`. | `tz-ready` is also bad. |
| `reconfigure` | No rollback — re-run that same chain against the live box. | A service died but the box is otherwise the team's to keep. |
| `rebuild` | Destroy the VM, re-clone it from its **box template**, configure from scratch, take both snapshots. Windows boxes are bootstrapped over the guest agent (`bootstrap_windows_box()`), Linux via cloud-init. | The VM is gone or won't boot. |
| `reset` | Per-box escalation ladder: `tz-ready` rollback → `tz-base` rollback + replant → golden rebuild. After every rung each box is probed (SSH via the gateway for Linux, guest agent for Windows, plus every scored port **from the engine** — Quotient's own vantage) and only the boxes left unhealthy escalate. A PAM preauth verdict escalates one rung (the `tz-base` disk is pre-plant) rather than jumping to rebuild. Prints which level each box ended at; exits non-zero if anything is still broken. | "Just reset it" — you don't know how deep the damage goes and don't want three round-trips mid-event. |
| `resync` | Pull the engine-authoritative secrets (event.conf, credlist, `/opt/quotient/.env`) into `.deploy_state.json`, then re-set the selected boxes' passwords via the guest agent. Touches nothing else. | Credentials drifted (partially-applied seed, manual box fiddling) and you need state and boxes back in line without a rollback. |
| `engine-recovery` | Re-clone the **engine VM** from the competition's engine template (`terraform apply -replace` on the engine resource only, `-target`-scoped). Team boxes, goldens, and the engine template are untouched; the scoring DB starts **EMPTY**. Ignores the box-selection flags. | The engine VM is broken or was deleted mid-event. Re-seed afterwards with `python3 create-competition.py --competition <id> --from-phase 7`. |

`--reset-event` (with `rollback-ready`/`rollback-base`/`reset`) additionally restarts the event from
the engine template — a fresh scoring DB and a phase-7 re-run — so scores reset and injects re-open
anchored at now. Use it for scrim reruns.

`rebuild` deliberately clones the **golden template** rather than the anchor team's live box, so a
rebuilt box carries the golden-stage installs without inheriting whatever the defenders did. This is
also what phase 4's apply #2 does at deploy time. The rebuild stamps the full ownership tag set and
the clone marker onto the new VM explicitly — a clone otherwise *inherits* its golden's tags, and a
golden reused across runs carries a stale `run-<id>` that would make teardown refuse the box. A
rebuilt box is recreated outside Terraform, so **every** rebuilt box (M3.3 made all
teams Terraform resources) drifts from Terraform state — the tool notes it per box, and the next
`terraform apply` will want to replace them. Fine mid-event; re-import or accept the replacement
afterwards.

`resync` cannot recover `box_password` (the box login) — that is baked into the boxes at
bootstrap and lives nowhere on the engine — so it aligns everything else and re-sets
credlist accounts + the box login it knows, reporting any box the guest agent couldn't reach.

`rollback-base` and `rebuild` also re-run the AD domain chain (`deploy_domain_configs()`) for
any selected box with a role in `domain_roles.json` — restoring a pre-Nakon disk undoes the
ADDS promotion/domain join too, so the affected team's DC is re-promoted (or its members
re-joined, if only members were reset) before `tz-ready` is re-taken. The stale ADDS
done-marker is deleted for a reset DC first, and if the domain chain can't run, `tz-ready` is
deliberately NOT re-taken (snapshotting then would bake a broken state in as 'as delivered').

`nakon deploy --only` is scoped to exactly the selected machines. *What* gets applied to each is
fixed by the content-addressed bundle (built from the full machine list), so a partial redeploy
plants the identical configuration set a full deploy would.

### Prerequisites

- Snapshots are taken by `create-competition.py` (`tz-base` for every box at the end of the phase-4
  block, `tz-ready` at the end of phase 6) and require a
  snapshot-capable `TF_VAR_datastore`: ZFS, LVM-thin, Ceph, or qcow2 on file storage. **Thick
  LVM cannot snapshot.** A range deployed before snapshotting existed, or on such a datastore,
  has only `reconfigure` and `rebuild`. The tool checks up front and prints which boxes are
  missing which snapshot rather than failing partway.
- Reads `teams.json`, `boxes.json`, `nakon-config.json` and the `box_password`/`box_creds` from
  `.deploy_state.json`. Those secrets must be the originals — the credlist accounts it recreates
  have to match what Quotient's `linux.credlist` expects, or a healthy box scores down on every
  auth-based check. `nakon-config.json` is regenerated deterministically if missing (the
  service/vuln sets are pinned by then).

### Caution

A rollback **discards everything the defending team did to that box.** It's a reset to a known
state, not a repair. Every mode except `reconfigure` says so and requires confirmation unless
`--yes`. Use `--dry-run` first.

## `destroy-competition.py`

> Module layout (0.2.0): the CLI is unchanged; the code is split into `destroy_gate_ops.py` / `destroy_sweep_ops.py` / `destroy_templates_ops.py` — see [internals.md](internals.md#destroy-competitionpy).

```bash
python3 destroy-competition.py                                # teams-only (M4 default)
python3 destroy-competition.py --competition <id> --full      # also destroy templates
python3 destroy-competition.py --competition <id> --full --end-of-competition  # frozen comp
python3 destroy-competition.py --competition <id> --skip-artifacts  # do not collect reports
python3 destroy-competition.py --competition <id> --artifacts-timeout 20  # per-file pull budget
```

Destroys all of the competition's team boxes, then runs `terraform destroy`. Every team is in
Terraform state, so `terraform destroy` alone removes them. Requires a `run_id` in
`.deploy_state.json` plus that competition's `teams.json` + `boxes.json` (all written by `deploy()`), and restores
the per-competition `TF_VAR_*` values first so Terraform address-matches the original apply.

**Before the first destructive call** it collects the run's test artifacts into
`competitions/<id>/.automated-tests/<run-id>/` — the red report from red01, blue's logs and report,
the engine capture, the harness evidence, then `REPORT.md`. This is the last moment it can: the
next calls purge clones and hard-stop every team box. It is also the safety net for a run whose
harness died, since teardown is the step that always happens. Collection **warns and proceeds**
(never blocks), so `--skip-artifacts` exists only to save time, and `--artifacts-timeout` bounds a
dead source at 45s per file by default. See
[automated-test-artifacts.md](automated-test-artifacts.md).

M4 teardown modes: the default is **teams-only** — team clones, the engine VM, and the
bridges die; the competition's golden templates and engine template are KEPT and the next
deploy reuses them by hash (test-run reuse). `--full` additionally destroys the templates
(clones strictly first — linked clones die with their base disks) and removes
`.template-hashes.json`. On a FROZEN competition `--full` refuses without
`--end-of-competition`, so an accidental full teardown mid-event is impossible.

### Run ownership: teardown only touches THIS deploy's VMs (2026-10-02)

Every deploy mints a per-competition-directory **run id** (`run-<hex>`, in
`.deploy_state.json`) and stamps it as a PVE tag on everything it creates. Teardown requires
the FULL ownership set (`tezcatlipoca` + `comp-<id>` + `run-<id>`) before it stops or deletes
anything — so **two worktrees deploying the same competition ID can no longer destroy each
other's VMs**. Behavior you will see:

- A VM tagged with the comp but a DIFFERENT run id (another worktree's run) is refused/skipped
  with a loud warning, everywhere: pre-stop, the leftover sweep, `--full` template destroys,
  and deploy phase-1 reclamation. It is never destroyed from the wrong worktree.
- An UNTAGGED VM on a computed vmid is refused — unless it carries this competition's clone
  marker (an interrupted clone whose tagging step never ran), which proves ownership. There is
  no escape hatch.
- A state file with no run id is refused by teardown before anything is collected or destroyed:
  ownership cannot be proven, so remove such a range by hand on the node.
- A deploy whose preflight finds comp-tagged VMs missing its run tag refuses: they are another
  worktree's run of the same competition ID — coordinate with that session.

`report_remaining` (printed when teardown cannot finish) classifies every survivor the same
way, so a human always knows what is ours, what belongs to another run, and what carries no
run tag.

**Lifecycle rule: tear the golden range down once the run has achieved its goal.** Teams-only
exists for the mid-run loop only — crash resume, iterate, re-run the SAME competition; that
is the only reuse a golden set supports. It can never serve a DIFFERENT competition: the
templates are tagged `comp-<id>` and every M4 hash input (box list, pins, box_password, ssh
key) is this competition's, so another competition's phase-1 hash gate would destroy and
rebuild them regardless. Once the goal is reached (validation pass done, event over, evidence
captured), run `--full` (add `--end-of-competition` if frozen) — leaving goldens behind just
strands templates on the datastore squatting on the competition's golden vmids.

Related M4 commands:

```bash
python3 verify-competition.py competitions/<id> --freeze --windows-domain-validated
python3 verify-competition.py competitions/<id> --unfreeze --confirm-unfreeze
python3 redeploy-competition.py --competition <id> --mode engine-recovery   # fresh engine from template
python3 redeploy-competition.py --competition <id> --teams team2 --mode rebuild --yes  # team rebuild
python3 verify-competition.py competitions/<id> --fix-round-loop            # unfreeze a stuck round loop
```

## Operational modes and one-off flags

Everything that exists in argparse but is not part of the happy path, in one place.

| Flag / mode | Script | What it does |
|---|---|---|
| `--mode resync` | `redeploy-competition.py` | Align `.deploy_state.json` with the engine-authoritative secrets (event.conf, credlist, `/opt/quotient/.env`), then re-set the selected boxes' passwords via the guest agent. No rollback. Cannot recover `box_password` (baked in at bootstrap; see internals) |
| `--mode engine-recovery` | `redeploy-competition.py` | Re-clone the engine VM from the competition's engine template, `-replace` on that resource only. Ignored box-selection flags; fresh **empty** scoring DB — re-seed with `--from-phase 7` |
| `--reset-event` | `redeploy-competition.py` | Valid only with `rollback-ready`/`rollback-base`/`reset`: after the rollback(s), also restart the event from the engine template and re-run phase 7, so scores reset and injects re-open anchored at now. Use for scrim reruns |
| `--end-of-competition` | `destroy-competition.py` | Required with `--full` on a **frozen** competition — makes an accidental mid-event full teardown impossible |
| `--windows-domain-validated` | `verify-competition.py` | Operator attestation that a Windows/domain lineup's run exercised DomainSID uniqueness, machine SIDs, and the three-pass ordering. Required alongside `--freeze` for such a lineup |
| `--fix-round-loop` | `verify-competition.py` | Issue the `POST /api/competition/start` + `POST /api/engine/pause {pause:false}` pair when the round loop did not auto-resume after an engine reboot |
| `--freeze` / `--unfreeze --confirm-unfreeze` | `verify-competition.py` | Write / remove the `.frozen.json` record. Freeze requires all gates PASS (incl. plant coverage) and must be the **last** thing you do before the event — committing afterwards trips the drift gate |
| `--expect-no-vulns` | `verify-competition.py` | Force the misconfig gates to SKIP (a comp with an empty `box_vulns.json` already skips them automatically) |
| `--allow-unverified <gate>` | `verify-competition.py` | Waive ONE gate's `SKIP` (could-not-evaluate) verdict so it does not fail the exit code. Repeatable; waives only a SKIP, never a FAIL; unknown names are warned about. Without it a gate that couldn't run is not a pass |
| `--timeout SECONDS` | `verify-competition.py` | Optional whole-run wall-clock budget (default `0` = off), checked **between** gates so it never interrupts a Proxmox task. A gate skipped on budget is recorded `SKIP` — non-passing — so a budget can bound a verify but never turn an unevaluated range into a PASS (waive with `--allow-unverified`) |
| `--packet <profile>` | `verify-competition.py` | Add the `packet_creds` + `packet_accounts` gates (packet credentials match `credentials.txt`; `out_of_scope` decoy accounts exist) |
| `--red-identity` | `verify-competition.py` | Prove red's **routed-mode** source address survives end-to-end: holds a TCP connection from red01 to a Linux box's `:22` and reads the box's `ss` table, expecting red's segment IP as the peer rather than the team gateway. Tri-state like the isolation check (SKIP when unverifiable). Requires red01 deployed |
| `--red-ip` / `--red-user` / `--red-seg-ip` | `verify-competition.py` | Inputs for `--red-identity`. Defaults: red01 `10.0.0.198`, user `sysadmin`, segment IP from `../bad-auto/config.yaml` else `10.200.0.10` |
| `--scoring-vmid N` | `create-competition.py` | Pick a free engine vmid for this run (dodges the default 1000 where it is taken) |

The red team's network mode itself (`routed` vs `masq`) is owned by `../bad-auto`, not this repo;
[architecture.md](architecture.md#red-team-identity-bad-auto-side-not-this-pipeline) describes both.

## `run-deploy.sh`

A minimal example wrapper around `create-competition.py`'s old stdin-driven interface, logging
to a gitignored file — not a documented interface in its own right. Prefer the flags above
directly; adapt the wrapper's `printf`/logging pattern only if you specifically need
stdin-driven prompt answers instead.

## Running a deploy that outlives your shell

A deploy takes tens of minutes to hours; a tool call, a terminal, and a harness background task do
not. Wrapping the deploy in a `timeout` to fit is the wrong fix — it turns a slow success into an
abrupt partial-state kill (six recorded exec logs open with a bare `Terminated`). Start it in its own
session with its own log, then poll **the log**, never the process:

```bash
cd <worktree-root>                     # every path resolves from here
mkdir -p logs
setsid nohup python3 -u create-competition.py \
    --competition <id> --teams <N> --scoring-vmid <free> --yes \
    > logs/<id>-$(date +%Y%m%d-%H%M).log 2>&1 < /dev/null &
echo "pid $! — follow with: tail -f logs/<id>-*.log"
```

- `setsid` puts the deploy in its own session/process group, so it survives the shell that started
  it; `nohup` + the redirected log means nothing is lost when the caller goes away.
- **Kill by process group or by pid** — `kill -TERM -<pid>` (negative == the group). Never
  `pkill -f`/`pgrep -f` a pattern that also appears in the invoking command line: it matches the
  caller itself and self-kills. That has happened twice on this project.
- Never combine "kill" and "relaunch" in one pattern; kill, confirm it is gone, then relaunch.
- In-process equivalent: `utils.spawn_detached(cmd, log_path)` returns `(pid, log_path)` for code
  that needs to start one.

`run-deploy.sh` remains the stdin-driven example wrapper; the recipe above is what a non-interactive
run should use.

## Deploying from a worktree

**RULE — any practice run (capacity test, canary, shakedown, any deploy whose goal is testing the
pipeline rather than hosting an event) MUST run from a NEW worktree cut off `main`.** The rule, its
rationale, the pre-flight copy list, and the env-var traps live in
[AGENTS.md → Practice runs](../AGENTS.md#practice-runs-must-run-from-a-new-worktree). A worked
example is [svc-matrix-2026-09-28-report.md](reports/svc-matrix-2026-09-28-report.md).

