# Scrim harness

Design notes for `run-agent-scrim.py` (lifecycle orchestrator), `scrim-report.py` (interaction scoring) and `red/tlaloc_ops.py` (the red deployment the scrim shares with phase 7). Numeric pass/fail gates: [rehearsal-gates.md](rehearsal-gates.md).

## Module map

The two scripts are thin CLIs over the `scrim/` and `scrim_report/` packages. Cross-module calls are module-qualified (`procs.run_tree`, `quotient_api.qget`), so tests patch where a function is *defined* (`mock.patch.object(procs, "run", ...)`). `log` is from-imported; patch it in the module under test.

| `scrim/` module | Responsibility |
|---|---|
| `cli`, `core`, `runfiles`, `run_manifest` | Flags and stage sequence; repo paths, timing constants, shared helpers; run-dir file names shared with the report; `run.json` and resume rules |
| `procs`, `supervisor` | Process-group supervision (`ScrimTimeout`); worker-thread supervision |
| `quotient_api`, `staging`, `compworld`, `fire_test` | Cookie jars and locks, `qget`/`_qlogin`; `stage_author`/`creds_from_files`/`stage_deploy`; per-comp facts and `verify_cmd`; `stage_verify` + web01 fire test |
| `blue_helpers`, `endpoints`, `blue_prompt`, `inject_sync`, `blue_agent`, `blue_watchdog` | Generated blue workdir files (`mybox`, `qlogin`, `score.py`, `myscore`, `submit-inject`, opencode config, notebook); `api_key`/`blue_ep`; cycle prompt and `scoreboard_delta`; `reanchor_injects`/`inject_brief`; `stage_blues`/cycle runner/`blue_feed_loop`; watchdog |
| `scoreboard_monitor`, `red_link`, `red_tunnel`, `llm_probe`, `red_stage` | `monitor_loop`; red01 ssh and state pull; `RedTunnel`; red-LLM hysteresis; `stage_red` (the phase-7 planter plus the pre-T0 gates) |
| `event_run`, `evidence`, `teardown_stage`, `test_folder` | `stage_run`; capture; `stage_teardown`; run test folder and report |

`scrim_report/`: `cli`, `loaders`, `host_labels`, `timefmt`, `red_side`, `blue_side`, `gate_table`, `render`.

## run-agent-scrim.py

Stages: author (optional) → deploy → verify + fire test → blue agents (opencode, fresh session per cycle) → red01 staged with tlaloc through the same planter phase 7 uses → feeds and monitoring to the deadline → evidence capture → teardown (red01 + range, unless `--keep-range`; templates kept with `--keep-templates`). The run dir defaults to `competitions/<comp>/.automated-tests/<run-id>/` (`--run-dir` overrides; recorded in `test.json.paths.run_dir`). Before red01 is destroyed the harness collects `RED-TEAM.md`, `BLUE-TEAM.md`, `REPORT.md`, `evidence/**` and folds scrim-report's verdict into `test.json` ([automated-test-artifacts.md](automated-test-artifacts.md)).

Design rules:

- Fresh opencode session per cycle with a compact prompt; exact SSH and inject-submit commands embedded in every prompt (agents failed to re-derive them); helper scripts materialized into each blue workdir.
- Blue sessions launch sequentially (concurrent opencode boots race its DB).
- One scrim per resolved range workspace: `cli.main` takes a flock under `~/.tezcatlipoca/locks/` before anything is staged and refuses a second scrim naming the holder. The deploy's engine lock covers engine-touching work only, and a `--skip-deploy` scrim takes none of it.
- Red reaches a public LLM endpoint directly from red01; a reverse tunnel exists for operator-side endpoints (local llama.cpp, a tailnet LLM host) only.

### Staging

- `RUNTIME_FILES` (`teams.json`, `estate-state.json`, `credentials.txt`, `nakon-config.json`, `packet.md`, `event.conf`) are regenerated per deploy and never copied into a new competition. `stage_author` also skips `.postclone-swept`, `LOG.md`, `sub-*`, `.nakon-domain-*`; re-copies `injects/`; rewrites the template's `Compfile` first line.
- `creds_from_files`: all secrets come from the state document (`estate-state.json`), `teams.json`, `credentials.txt`.
- `stage_deploy` writes full stdout plus a `=== STDERR ===` section to `deploy.log` for every deploy, success included; the console tail is too short. Success requires `last_phase: 8`/`Deploy complete` in stdout or state.
- `stage_verify` fire test: stop nginx on team1 web01, wait 150 s, expect team1 DOWN on the scoreboard **and** a dead HTTP probe from the engine's path (the scoreboard alone can lie). Restore with unmask + start, up to 3 attempts, then require scoreboard-up and a live probe. Any miss aborts with the manual fix; failing closed before T0 is cheap. Other verify failures stay warnings.

### Quotient sessions

> The 2026-10-18 live probe did not reproduce Quotient's one-session-per-account eviction. The harness still shares one jar per account and avoids ad-hoc logins: that is simpler and works either way.

- Nobody logs in ad hoc; `./qlogin` runs only after `{"error":"Forbidden"}`. All clients share one jar per account (blues/monitor `/tmp/jar.team<n>`; capture `evidence/.jar-admin`); a re-login is harmless if eviction is not enforced, and still converges if it is.
- `qget` re-logins into the shared jar and retries once on an error. `score.py` reads the Netscape jar (session cookie on the last non-comment line) and shells out to `./qlogin`; `submit-inject` resubmits after one `./qlogin`.
- `_team_tid`: the services API keys on the engine's IDs (1, 2, …), not the subnet identifier (101, 102); the latter returns `Forbidden` even with a valid cookie.
- `parsed_status` raises so callers choose; `status_text` renders a self-diagnosing line (`scoreboard unreachable (TypeName: msg)`).

### Blue agents

- `stage_blues`: opencode's base prompt is ~20k tokens, so the advertised context must leave room or it self-compacts fatally. Local endpoints: context 60000, output 4000, no `reasoning_effort`. Cloud: 120000/16000 plus `--reasoning-effort` (`effort_json`).
- `api_key`: resolved from the endpoint table (`scrim/llm_endpoints.py`): a public endpoint requires its own key env (`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, …) and raises if it is missing; an operator-side endpoint gets `OPENROUTER_API_KEY` when set, else the literal `local`. Red's key is pushed separately to red01 as `TLALOC_LLM_API_KEY` in `/etc/tlaloc/env`.
- `_opencode_run` launch hardening: `stdin=DEVNULL`, own process group, per-team `HOME`/XDG under `blue-team<n>/.opencode-home/` (state DB and logs land in the run dir). The child is `bash -c 'exec opencode run …'` with the prompt in `$CYCLE_PROMPT`, because opencode's server dies silently when exec'd straight from Python (the shell parent is the working differentiator). It is a `Popen` whose timeout path `os.killpg()`s the session: `subprocess.run(timeout=…)` hung ~80 min because opencode's server grandchild held the pipes and the shared lock. `_opencode_log_tail` appends the server-log tail to failed cycles.
- `blue_feed_loop`: serialized per **endpoint** (`stage_run` builds one lock per distinct base URL). Only team2 has its own endpoint flag (`--blue2-base-url`/`--blue2-model`); every other team shares the default, so decide the split before launch. Throughput: at 8 teams each team gets a cycle about every 10 minutes, the biggest lever on blue's score. One immediate retry per failed cycle. Each cycle keeps its prompt and transcript (`cycles/cycle-T+NNN.{prompt.txt,output.log}`); `feed.log` is a 2000-char tail.
- Pacing: sleep = clamp(30..600 s, `CYCLE_TARGET_PERIOD` 480 − actual cycle time); `CYCLE_TIMEOUT` 600 s per run (`TEZ_CYCLE_TIMEOUT` overrides it for a slower endpoint).
- `stage_run`: blues staggered (team1 T+0, team2 +300 s) **and** lock-serialized; both are needed because the shared local endpoint has few slots and rejects oversized prompts.
- `finalize_blue_narratives` runs before capture: a team whose cycles timed out, crashed or never started has no REPORT.md, so the harness files its own record (cycle rc= lines, `LOG.md`, scoreboard delta) as that team's REPORT.md, labelled harness-synthesized. The collector's "present agent, missing canonical document" warning stays as the guard for a run that died before this point.
- `blue_cycle_prompt`: notebook first, then at most two change/fix actions (read-only investigation is unbounded, since hunts are where blue learns), then stop. Priority: restore > inject due within 30 min > one hunt-checklist item. States a ~25-minute budget with wrap-up by minute 20. ROE in every prompt: never attack the engine (`$ENGINE_IP`); never change the scoring-check accounts (triage/svc-imaging/wardops).
- `scoreboard_delta`: new DOWN → "RESTORE IT FIRST"; back UP → "expect red to re-attack it"; still DOWN annotated with since-T+.
- `inject_brief`/`due_in_min`: unsubmitted and due ≤30 min is flagged "TASK #1". The engine's timezone isn't guaranteed, so either UTC or local interpretation is accepted if it lands within −15 min … +12 h.
- `NOTEBOOK_TEMPLATE`: SNAPSHOT (current state + next action) / OPEN INCIDENTS / HUNT CHECKLIST / DONE. Embedded in each prompt (2500 chars) with the last 5 `LOG.md` lines; the update is cycle step 0. `NOTEBOOK.md` persists across reruns; `LOG.md` restarts.
- `MYBOX`: Linux over key auth, Windows over password (sshpass), through the engine ProxyCommand.

### Monitoring and clock

- `monitor_loop` snapshots every `MONITOR_INTERVAL` (60 s) from T+0 (down-minute math needs the full window). `scoreboard-state.jsonl` is the structured source of truth; `monitor.log` is human-readable. Red's `events.jsonl` and newest report are pulled every snapshot so a teardown-time scp flake can't lose them.
- t0 is captured **after** `stage_red` returns: red setup can take 5–30 min and mustn't count against `--duration-min`. The T0 log line prints the out-of-scoring setup time.
- The window is authoritative: `supervise_workers` sets `stop` at `t0 + duration_min*60` and joins a worker still inside a cycle only for what is left of the window plus `EVENT_END_GRACE` (120 s); `_opencode_run` aborts the opencode session as soon as `stop` is set. Capture and teardown therefore run at the deadline even when a cycle would have overrun it.
- `reanchor_injects`: inject offsets resolve at deploy time, so a `--skip-deploy` restart or long staging gap reaches T0 with injects expired. Right after t0 the offsets are re-resolved at now and pushed via UpdateInject (`POST /api/injects/{id}`), matched by title, re-listing every attachment under keep-files (unlisted ones are deleted). Unconditional and idempotent; unordered offsets abort before the event.

### Red

Before T0, `stage_red` requires, in order: a seed manifest proving a planted stage (`verify_tlaloc_seeded`), an LLM endpoint red01 itself can dial (`check_red_llm`), and a reachable box on every team (`verify_red_reaches_teams`). The last is judged from the `red_identity`/`red_teams` summary lines only, not from verify's exit code: the full run's rc also carries unrelated estate gates.

- Posture: the competition's `red.*` block (Compfile keys or its `red_yaml` file) supplies `assume_breach`, `depth`, `duration_min`, `ladder`, `caps`, `windows` and `budget`; the rest is tlaloc's default (`decision_window_min` 4 with ±15% jitter). The scrim records its own pacing caps (start 2, end 4, endgame 6) in the run manifest, and the report judges `max_simultaneous_down` against the end cap (4), not a hardcoded threshold.
- Red01's slot is per competition. `--red-vmid` else Compfile `red01_vmid`, else the recorded placement, else one golden stride past this placement's last golden band; `--red-ip` else Compfile `red01_ip`, else the recorded placement, else a free address selected from the red band `10.0.0.240–10.0.0.249`. An occupied vmid is refused unless this run owns it, and an IP answered by a running guest is refused; an unverifiable address is refused unless it is already recorded for this range or set in the Compfile.
- **LLM gate**: operator-side reachability proves nothing about what red01 can reach. `--red-tunnel` (default `auto`; on for operator-side endpoints, ignored for a public API) runs a reverse SSH forward — red01 dials `localhost:8180`, or the relay's own port when the URL already names localhost — with `ServerAliveInterval` 15 s and a supervisor restart. Red01's pushed config carries the tunnel-local URL. Post-deploy, `check_red_llm` curls `/models` from red01 (direct, then via the engine jump); 200/401/403 all mean the endpoint answered (the probe is deliberately keyless), and any other result aborts before T0. `monitor_loop` repeats it every tick (`red_llm_watch`): one failure logs, three consecutive failures restart the tunnel once and write a durable alert, and recovery clears the episode.
- The day-0 seed runs `tlaloc seed` on red01 from `/opt/tlaloc`; the agent writes a per-box manifest at `/var/lib/tlaloc/intel/red/manifest.json` and the deploy pulls it to `<comp>/red/manifest.json`. `verify_tlaloc_seeded` requires that manifest to be readable and to show a stage on at least one box — no manifest, or no seeded box, refuses T0.
- State dir: `/var/lib/tlaloc` on red01 (the pushed `run.state_dir`), holding `world.json`, `events.jsonl`, `report.md`, `restore-table.json`, `report-secrets.md` and the `intel/` tree the deploy pushed. There is no operator-side red state dir.
- Identity and reachability: red01 keeps its red-segment source (`10.200.0.10`) to the engine-node teams; a satellite jump SNATs red to the team gateway. `verify-competition.py --red-identity --red-teams all` is the third pre-T0 gate. There are no red mode/subnet/template/storage flags: the private bridge, the segment and the engine rules belong to the deploy.
- `pull_red_state` (`scrim/red_link.py`) waits up to 90 s for red's `report.md`, stages the root-owned artifacts through `/tmp/ba`, and scps them into `run_dir/evidence/red/` — direct first, then via the engine jump. `pull_red_snapshot` is the in-run best-effort variant every monitor tick (`events.jsonl` plus red's newest report; 75 s timeout, never raises).
- `_red_ssh_ctx`: red01's address is `--red-ip` when given, else this competition's recorded identity (the state/placement `red01` record); never a sibling checkout's config. The same `_red_ssh_spec` feeds `record_red_agent`, so the harness and the teardown collector dial one address.
- Liveness has an out-of-band signal: `red_box_power_state` asks Proxmox (no guest networking), and `red_liveness` is printed on every failed guest-path probe — the monitor's snapshot warning, `check_red_llm` (including an ssh that never connected), and the red-LLM alert's `liveness` field. "red is dead" and "the path to red is dead" therefore read differently in the run log (caught live: they used to read identically and the fix triage went down the wrong branch).

### Capture and teardown

- `stage_capture`: re-logins on `Forbidden`, falls back to the team account if admin is locked out, best-effort pauses scoring, freezes `scoreboard-state.jsonl` into evidence, archives blue LOG/NOTEBOOK/feed.log, `sub-*` deliverables, `submissions/`, `cycles/`. It dumps `evidence/final-scoreboard.json` (teams, injects, per-team service states) **before** pausing, because teardown destroys the scoring DB and the report's "Final scores" reads that dump.
- `stage_teardown`: stops the LLM tunnel, pulls red's state and runs the artifact collector + `INTERACTION.md` so the report grades the final world; then `teardown_red` captures `tlaloc report`, pulls the whole state dir into `<comp>/red/`, stops `tlaloc.service` and destroys red01 through the run's ownership tags, failing if the vmid survives. The same terminal teardown the `destroy-competition.py` CLI runs is executed in-process unless `--keep-range`. `--keep-templates` runs it with the golden + engine templates kept: team boxes, engine, bridges and red01 all go, and the next run of this competition reuses the templates by the hash stamped on the template itself. A practice cycle that keeps its templates therefore hands the event the exact artifacts it verified — see [internals.md](internals.md#template-lifecycle-engine-template_freeze-py-engine_template_ops-py).

### Mid-run box resets

Reset a dead box from a second shell while the harness runs:

```bash
python3 redeploy-competition.py --competition <id> --teams N --boxes web01 --mode reset --yes
```

The engine lock it takes serializes only other engine-touching operations. The box's services go DOWN during the reset and blue's watchdog may flag it (expected). Replant/rebuild rungs retake `tz-ready`. `--reset-event` composes with `reset` for full reruns.

## scrim-report.py

Reads a run dir (`evidence/red/events*.jsonl`, `world.json`, `blue-team*/{feed.log, LOG.md, NOTEBOOK.md, REPORT.md, submissions/, sub*.md}`, `scoreboard-state.jsonl` if present), writes `INTERACTION.md`. The harness runs it at teardown before red01 is destroyed; when the harness never got there, the artifact step runs it (`artifacts_lib/verdict.ensure_verdict`), and `artifacts_ops.ingest_verdict` folds the verdict and gate table into `test.json`; `REPORT.md` quotes it.

- **Score** = restorations + red restore-reactions + evictions + injects + eradication. A run scoring 0 is a failed run regardless of red's log: "parallel monologues. Red and blue never touched the same game."
- Constants: `STALL_SEC = 600` (stall = ≥10 min without a new successful action); `REAKILL_GAP_SEC = 900` (re-attack after ≥15 min implies a restore); `RESTORE_TTR_GATE_MIN = 15`. `GATES` is the machine source for [rehearsal-gates.md](rehearsal-gates.md).
- `parse_ts`: event timestamps are red01's clock, treated as UTC.
- `load_red_events` merges the final `events.jsonl` with in-run snapshots, filtered to [T0 − 120 s, T0 + 8 h] and deduplicated (ts/kind/tactic/target/detail). T0 is `run.json`'s `t0`, else `world.json` `meta.event_start` under `evidence/red/`.
- `load_scoreboard` returns `[]` without `scoreboard-state.jsonl`; the report then runs in no-scoreboard mode (down-minutes, TTR, max-simultaneous-down are n/a; restorations inferred from re-kills and marked).
- `host_label` maps any third octet 101–199 to team X−100; last octet 2=dc01, 3=win01, 4=web01, 5=app01, 6=db01.
- `takedown_fields` prefers structured `data` (ip/unit/service/mode), falls back to regexing the detail string; mode defaults to "firewall" for `impact_firewall`, else "stop". `foothold_list` accepts dict-by-ip or list.
- Red stalls are gaps between successful non-`health_check` actions, and the opening (T0 → first success) counts. With no clock (no `world.json`/`event_start`) stall math is skipped rather than aborting the report (the `timeouts` count feeds a gate).
- `windows_footholds` comes from `world.json`'s `windows` flag, else is inferred from initial-access events against Windows boxes' last octets in `boxes.json`.
- Evictions are not a red gate: red's health checks only see SSH-class footholds, so red-observed evictions read 0 and that absence is an observability limit, not a failed red. Blue's self-reported `EVICTED:` lines are counted separately as `evictions_logged` and are never used as red evidence. The interaction score still adds red-observed evictions.
- `down_windows` per team: real DOWN→UP transitions between snapshots, TTRs, down-minutes, max simultaneous down; a service down at the last snapshot closes at event end and counts.
- `blue_metrics`: cycles rc=0 from `feed.log` `===== cycle ... rc=N =====` headers ("MANUAL" counted separately); notebook entries = non-heading `LOG.md` lines + checked `NOTEBOOK.md` boxes; eradication = log lines matching a persistence/foothold indicator (accounts, keys, drop-ins, cron, preload, SUID, scheduled tasks, hidden attributes) AND a removal verb (removed/deleted/disabled/uninstalled/locked/changed-back/reset); injects = `sub*.md`/`sub*.txt` (the documented `sub-<id>` form plus what agents actually wrote) and everything under `submissions/`.

## red/tlaloc_ops.py

The one red deployment path: phase 7 calls `plant_assume_breach(ctx)` and `scrim/red_stage.stage_red` calls the same function, so a scrim cannot stage a red the deploy would not.

- Reads the competition's `red.*` block (Compfile keys or its `red_yaml` file), validates it, and renders `.tlaloc-config.yaml`; the file pushed to `/etc/tlaloc/config.yaml` is that block over tlaloc's defaults. `verify_config_contract` re-reads the pushed file and refuses any field the deploy meant to set that it dropped.
- Deploys red01 through `red/red01_ops.py`: clone or adopt this run's VM, pin both NICs, grow the disk to `red.disk_gb` (default 60 GB), install the agent's apt dependencies. red01 carries the run's ownership tags.
- C2: with Mythic enabled (the default; `red.c2 off` opts out — the Compfile's `red.c2_ip`, else red01), Gaia installs and configures Mythic and the competition's operation, `tlaloc c2-prep` freezes the payload matrix, and the engine gets the red-segment rules plus the C2 DNAT (re-asserted every 30 s by `range-red-firewall.timer`). A failed C2 step aborts the deploy.
- Stages `tlaloc/` at `/opt/tlaloc` (staged, not installed: the unit is what puts it on the import path), pushes the competition's intel files, the config and `/etc/tlaloc/env` (the LLM key), installs `tlaloc.service`, runs the day-0 seed, and pulls the seed manifest to `<comp>/red/manifest.json`.
- Verdict: `tlaloc report` writes `report.md` and `restore-table.json` on the box; `pull_evidence` tars `/var/lib/tlaloc` into `<comp>/red/` and writes `verdict.json` (summary, entry and command counts). `RED-TEAM.md` is filed from `report.md`.
