# Usage — agents

Non-interactive driving: flags, pre-authored configs, machine-readable validation. One-time Proxmox/template setup is in [usage-people.md](usage-people.md#prerequisites-one-time-per-proxmox-host). Central runs + MCP: [tezcatlipoca-dashboard](https://github.com/DawgSecTeam/tezcatlipoca-dashboard) (address/token: `infrastructure-status.md`, gitignored).

## `create-competition.py`

| Flag | Effect |
|---|---|
| `--competition NAME` | Deploys straight away if `Compfile` + `boxes.json` exist; otherwise creates it |
| `--teams N` | 1–154 (`MAX_TEAMS`) |
| `--yes` | Skip the confirm prompt |
| `--scenario TEXT`, `--difficulty N` | New competitions only (difficulty 1–10) |
| `--box-username NAME` | New competitions only; default `ubuntu`. Written to `users.json` |
| `--credlist-usernames A,B,C` | New competitions only; exactly 3; default `admin,user1,user2` |
| `--scoring-vmid N` | Engine vmid (default 1000) |
| `--from-phase N` | Resume at phase N; `N > 1` skips phase 1's cleanup and phase 2's create |
| `--force-from-phase` | Allow `--from-phase` past phases the state never saw complete (known-stale checkpoint only) |
| `--plan-only` | Generate config and print the summary; touches no infrastructure |

Box selection (`collect_boxes()`) is always interactive. To avoid it, pre-author the competition. Run `--plan-only` before any real deploy; `--yes` otherwise walks straight into the deploy.

### Resuming

Phases: [architecture.md](architecture.md#phases). A failed deploy prints its phase and the resume command. Each phase writes `last_phase`; `--from-phase N` is refused when `N > last_phase + 1` (a missing `last_phase` reads as 0), and when the state's `pipeline_version` differs.

Phase 8's steps (`seeded`, `engine_unpaused`, `injects_created`) are flag-gated in the state document (`estate-state.json`); re-unpausing is unsafe and re-creating injects duplicates them. To force a step, clear its flag first.

## Pre-authoring

```
competitions/<id>/Compfile     # name, scenario, difficulty
competitions/<id>/boxes.json   # name/template/cpu/memory_mb/disk_gb/last_octet
competitions/<id>/users.json   # optional: {"box_username": "...", "credlist_usernames": [3 names]}
```

Shape: `competitions/example/`. Then `python3 create-competition.py --competition <id> --teams N --yes`.

### Pinning

By default services/misconfigs are randomized, then written out so a rerun reproduces them. To choose them, write either file, keyed by box **type** (every team gets the same set so Quotient's wildcard checks hold):

```
box_services.json   {"web01": ["apache"]}
box_vulns.json      {"web01": ["suid-find", ...]}
```

Entries are strings or `{"name": ..., "vars": {...}}` (see `vendor/nakon/config-example.json`). `ADDS` and `Domain Join` never go here; they are driven by `deploy_domain_configs()` from `domain_roles.json`.

```bash
cd vendor/nakon
python3 -m nakon catalog list --json
python3 -m nakon catalog check --box-vulns /abs/path/competitions/<id>/box_vulns.json
```

`catalog check` catches typos, building blocks requested instead of their wrapping misconfig, platform mismatches, and missing vars. The pinned submodule shows mechanism only; check the remote catalog before concluding something is unsupported.

## `generate-packet.py`

`python3 generate-packet.py competitions/<id>` renders `packet.md`: a team-agnostic briefing for competitors. Local read of `Compfile`, `boxes.json`, `box_services.json`; never reads `box_vulns.json`; overwrites in full.

## `verify-competition.py`

```bash
python3 verify-competition.py competitions/<id> [--engine-ip IP] [--admin-password=PW] [--strict-services] [--allow-unverified GATE]...
```

Use `--admin-password=PW` (equals form) so a leading `-` isn't parsed as an option.

| Gate | Checks |
|---|---|
| LOGIN | Every team and admin `POST /api/login` returns 200 |
| SERVICES | Services UP in the latest round. Informational unless `--strict-services` |
| ISOLATION | The `FORWARD` DROP is present, and with 2+ teams a cross-team connection is blocked while the target is provably alive and the source still reaches the internet. Unprovable → SKIP |
| MISCONFIG | A planted misconfig is present on a target; every team's copy of each verifiable config is identically present (some-but-not-all and none-anywhere both FAIL) |
| INJECTS | Engine inject count matches `injects/` |
| `pins_registered` | Every pinned service's `<box>-<Display>` check exists in Quotient |
| `plant_coverage` | Reads `plant_coverage_failed` from state; FAILs if never recorded. Golden plants record `{box}-golden` keys, which map onto every clone |
| `degradations` | Reads the deploy's tolerated-failure ledger from state; FAILs if never recorded or if ANY entry is present (an assume-breach red that never landed, a box that never answered, a failed apt prep). The deploy continues past these; verify does not pass them |
| `domains` | AD promotion, joins, AD plants, cross-team DomainSID uniqueness; fail-closed on a malformed `domain_roles.json` |
| `red_identity` | With `--red-identity`: red's routed source address survives to the boxes (`--red-ip`, `--red-seg-ip`, `--red-user`; defaults from `.badauto-config.yaml`, else red01 `10.0.0.198`, `sysadmin`, `10.200.0.10`) |
| `packet_creds`, `packet_accounts` | With `--packet <profile>`: published credentials match `credentials.txt`; `out_of_scope` decoy accounts exist |
| `round_loop` | Rounds are advancing and the latest is fresh |
| `firewall_in_path` | Per team: engine routes `192.168.<id>.1` via `172.31.<id>.2`, the firewall answers on `:22`, the engine no longer holds `.1`. Non-gating SKIP without a firewall |

**Gates are PASS / FAIL / SKIP; SKIP is non-passing.** Summary and exit code come from the same results. Waive a SKIP (never a FAIL) with `--allow-unverified <gate>`; unmatched names are warned. Structurally inapplicable cases (no `nakon-config.json`, `domain_roles.json`, or `injects/`; `--expect-no-vulns`; a comp planting no misconfigs, which auto-SKips misconfig as informational) are shown `[informational]` and don't gate. Service DOWN is not fatal; isolation never downgrades.

Other flags: `--expect-no-vulns` (skip misconfig gates for a packet comp without `box_vulns.json`); `--fix-round-loop` (POST `/api/competition/start` and `/api/engine/pause {pause:false}`; still FAILs, rerun to confirm); `--freeze` (records the freeze in the state document; needs all gates PASS; do it last, since later commits trip the drift gate); `--unfreeze --confirm-unfreeze`.

Inputs: engine IP from the state document (`estate-state.json`; `--engine-ip` overrides), team creds from `teams.json`, admin password from `credentials.txt`, SSH from the config (`estate/config.py`), planted configs from `nakon-config.json`. On a fresh deploy, team identifiers come from `TF_VAR_team_identifiers`, then saved local identifiers, then packet-profile suggestions, then the 101+ defaults; that fallback is not an availability check.

## `redeploy-competition.py`

Restores a subset of a live range's boxes without teardown.

```bash
python3 redeploy-competition.py --competition <id> [selection] [--mode M] [--dry-run] [--yes]
```

Selection (AND-combined; none = every box; no match is a hard error): `--teams 2,team3,104` (key, number, or subnet identifier), `--boxes web01,db01`, `--platform linux|windows`. `--dry-run` prints targets and which snapshots exist.

### Redeploy modes

| Mode | Does | Use when |
|---|---|---|
| `rollback-ready` (default) | Roll back to `tz-ready`, restart, wait for SSH | Box was fine at hour zero |
| `rollback-base` | Roll back to `tz-base`, rerun DNS/auth/`nakon deploy --only`/hardening, retake `tz-ready` | `tz-ready` is also bad |
| `reconfigure` | No rollback; rerun that chain on the live box | Service died, box otherwise keepable |
| `rebuild` | Destroy, re-clone from the **golden template**, configure, retake both snapshots | VM gone or won't boot |
| `reset` | Per-box ladder: `tz-ready` → `tz-base` + replant → golden rebuild. Probes after each rung (SSH or guest agent, plus every scored port from the engine); only unhealthy boxes escalate. A PAM preauth verdict escalates one rung. Exits non-zero if anything is still broken | Unknown damage depth |
| `resync` | Pull engine-authoritative secrets (`event.conf`, credlist, `/opt/quotient/.env`) into state, re-set selected boxes' passwords via guest agent | Credential drift. Cannot recover `box_password` (baked in at bootstrap); reports unreachable boxes |
| `engine-recovery` | Destroy the engine VM and re-clone it from the engine template through the estate component (scoped to the engine vmid by construction). Ignores selection flags. Scoring DB starts **empty** | Engine broken or deleted. Then `create-competition.py --competition <id> --from-phase 7` |

`--reset-event` (with `rollback-ready`/`rollback-base`/`reset`) also restarts the event from the engine template and reruns the seed, so scores reset and injects re-anchor at now.

- `rebuild` clones the golden template, not a live box, so defenders' changes aren't inherited. It stamps the full ownership tag set and clone marker explicitly (a clone inherits its golden's tags, and a stale `run-<id>` would make teardown refuse it).
- `rollback-base` and `rebuild` rerun the AD chain for selected boxes with a `domain_roles.json` role (a pre-nakon disk undoes promotion/joins). The stale ADDS done-marker is cleared first. If the chain can't run, `tz-ready` is not retaken.
- `nakon deploy --only` scopes to the selected machines; content comes from the bundle built from the full list, so partial and full redeploys plant identical sets.

Prerequisites: `tz-base`/`tz-ready` snapshots (a snapshot-capable datastore; thick LVM has only `reconfigure`/`rebuild`; missing snapshots are listed up front). Reads `teams.json`, `boxes.json`, `nakon-config.json` (regenerated if missing) and the original `box_password`/`box_creds` from state; they must match Quotient's `linux.credlist`.

Every mode except `reconfigure` discards the team's changes to the box and confirms unless `--yes`. Dry-run first.

## `destroy-competition.py`

Thin CLI over `teardown/` (Ownership, Inventory, step plan, journaled engine, `verify_clean`).

```bash
python3 destroy-competition.py --competition <id>                    # full teardown (default)
python3 destroy-competition.py --competition <id> --teams-only       # mid-event; keep templates
python3 destroy-competition.py --competition <id> --end-of-competition  # required if frozen
python3 destroy-competition.py --competition <id> --artifacts-timeout 20
```

Full teardown removes team boxes, engine, bridges, golden and engine templates (clones first; the state document's template records go too). **Exit 0 = estate verified clean** for this deploy's ownership set: the step plan completed, every hosting node was listable, nothing of ours is running or tagged, and red01 is gone — evidence archived before any of it. Frozen competitions refuse without `--end-of-competition`. `--teams-only` is for this competition's next run only; once the run's goal is met, run the full teardown, since goldens can't serve another competition (hash inputs and `comp-<id>` tags are per-competition) and otherwise squat on its golden vmids.

Reuse keys on the `tezcatlipoca-m4 hash=` description on the template itself, so the next deploy adopts kept templates even from a **fresh worktree or dashboard workspace** — where the state document does not exist, because the dashboard's workspace seeder deliberately strips it. So `--teams-only` (or the harness's `--keep-templates`) followed by a deploy of the same competition gives the event the exact templates the practice run tested, provided the hash inputs are unchanged; a config change rebuilds, and a frozen competition still refuses config drift. The `--teams-only` teardown must run from the worktree that holds the deploy's state, but the reuse it enables does not.

Requires a `run_id` in the state document (`estate-state.json`, or a legacy `.deploy_state.json`) plus `teams.json` and `boxes.json`; it activates the recorded placement/host first so every node-scoped call addresses the right PVE.

Before the first destructive call it captures the engine's final scoreboard when the harness never did (the scoring DB does not survive the estate step) and collects the run's artifacts into `competitions/<id>/.automated-tests/<run-id>/` (red report from red01, blue logs, engine capture, harness evidence, `REPORT.md`). Collection warns and proceeds, bounded per file by `--artifacts-timeout` (default 45 s); a `failed` transfer gets one retry, and whatever is still lost is printed as a note under the verification section rather than failing the destroy. There is no skip flag; on resume the journal skips it. See [automated-test-artifacts.md](automated-test-artifacts.md).

### Run ownership

Each deploy mints a `run-<hex>` id (in `estate-state.json`) and tags everything it creates. Teardown requires the full set (`tezcatlipoca` + `comp-<id>` + `run-<id>`):

- Same comp, different run id → refused with a warning everywhere (preflight, pre-stop, leftover sweep, template destroys).
- Untagged VM on a computed vmid → refused, unless it carries this competition's clone marker.
- State without a run id → refused before anything is collected; remove by hand.
- A deploy whose preflight finds comp-tagged VMs lacking its run tag refuses; coordinate with the other session.

`verify_clean` classifies survivors: ours (a problem), another run's (a note, untouched), untagged (a note).

**The workspace that owns a run is the only thing that can reclaim it.** A deploy is ownership-scoped,
so a range outlives its run id only if the workspace holding `estate-state.json` goes away while the
range is still up. Preflight then sees a comp-tagged range it cannot prove is its own and refuses
every later deploy of that competition, and no destroy path can claim it (the tag set in the VMs
carries the run id nothing local remembers). So always run `destroy-competition.py` — or
`--teams-only`, which keeps the templates — from the run's own workspace *before* discarding it. The
dashboard's per-run workspaces are the mechanical form of that rule; a manual run must do it by hand.

## Flags outside the happy path

| Flag | Script | Effect |
|---|---|---|
| `--end-of-competition` | destroy | Full teardown of a frozen competition |
| `--teams-only` | destroy | Keep this competition's golden + engine templates for its next run |
| `--keep-templates` | scrim | Same, from the harness: tear down the range but keep the templates |
| `--freeze` / `--unfreeze --confirm-unfreeze` | verify | Write/remove the freeze record in the state document |
| `--fix-round-loop` | verify | Start/unpause POSTs when the round loop didn't resume after an engine reboot |
| `--expect-no-vulns` | verify | Force misconfig gates to SKIP |

Red's network mode (`routed`/`masq`) is owned by `../bad-auto`; see [architecture.md](architecture.md#network-and-isolation).

## Central runs (the dashboard)

Range runs are submitted to the central dashboard
([tezcatlipoca-dashboard](https://github.com/DawgSecTeam/tezcatlipoca-dashboard); address and
token: `infrastructure-status.md`) through its web UI or its MCP tools (`submit_run`, `watch_run`,
`get_run_events`, `cancel_run`, `get_run_log`, `get_artifacts`). The dashboard cuts a
per-run engine worktree at the pinned ref and runs the same drivers against a per-run
workspace (`--comp-dir`), so sessions never contend over a checkout and never eat each
other's state. `watch_run` subscribes the session: stage events (deploy `[N/8]` phases,
scrim stages) are pushed as they complete — wait for the push instead of polling logs,
and use `get_run_events(since=N)` to recover anything missed across a reconnect. A second
run of the same competition is refused at submit (parallel elsewhere, fail-fast on real
collisions).

## Long-running deploys

Manual/recovery only — range runs go through the dashboard above. A deploy outlasts tool
calls, terminals, and harness task records. Don't wrap it in `timeout`. Detach it and poll the log, not the process:

```bash
cd <worktree-root>
mkdir -p logs
setsid nohup python3 -u create-competition.py \
    --competition <id> --teams <N> --scoring-vmid <free> --yes \
    > logs/<id>-$(date +%Y%m%d-%H%M).log 2>&1 < /dev/null &
```

Kill with `kill -TERM -<pid>` (process group). Never `pkill -f` a pattern that appears in the invoking command line. Kill, confirm gone, then relaunch.

Manual practice runs must use a new worktree: [AGENTS.md](../AGENTS.md#practice-runs-must-run-from-a-new-worktree).
