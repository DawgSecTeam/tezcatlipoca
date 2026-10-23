# Scrim harness

Design notes for `run-agent-scrim.py` (lifecycle orchestrator), `scrim-report.py` (interaction scoring) and `red/tlaloc_ops.py` (the red deployment the scrim shares with phase 7). Packages: `scrim/`, `scrim_report/`. Numeric gates and what each means: [rehearsal-gates.md](rehearsal-gates.md); thresholds live in `scrim_report/gate_table.py` (`GATES`) and `scrim_report/red_side.py` (`STALL_SEC`, `REAKILL_GAP_SEC`, `RESTORE_TTR_GATE_MIN`).

Cross-module calls are module-qualified (`procs.run_tree`, `quotient_api.qget`), so tests patch where a function is *defined* (`mock.patch.object(procs, "run", ...)`). `log` is from-imported; patch it in the module under test.

### Validated practice recipe

The strongest practice runs use **one team, 45 minutes, `deepseek-v4-pro` at the local
`http://10.0.0.143:8180/v1` endpoint, on a four-box/9-service competition** (for example
`practice-4box-2026-09`). A 2-team/30-minute run on the small two-box/3-service estate
serialises blue cycles and leaves red too few re-kill windows: it can pass the blue gates
but not `restore_reactions` and it frequently records an opening stall. The local
`8180` endpoint advertises a 1M context; the older `8080` llama.cpp endpoint advertises
16k and the harness refuses it for blue. When reusing `practice-4box`, clean its stale
pins first — `IIS HTTP` expects `W3SVC` and `iis-webshell` expects Defender, neither of
which the current Windows base template installs.

## run-agent-scrim.py

Stages: author (optional) → deploy → verify + fire test → blue agents (opencode, fresh session per cycle) → red01 staged with tlaloc through the same planter phase 7 uses → feeds and monitoring to the deadline → evidence capture → teardown (red01 + range, unless `--keep-range`; `--keep-templates` keeps the golden + engine templates, which hands the event the exact artifacts the practice run verified — [internals.md](internals.md#template-lifecycle-engine-template_freeze-py-engine_template_ops-py)). Run dir: `competitions/<comp>/.automated-tests/<run-id>/`, `--run-dir` overrides. Before red01 is destroyed the harness collects `RED-TEAM.md`, `BLUE-TEAM.md`, `REPORT.md`, `evidence/**` and the scrim verdict into `test.json` ([automated-test-artifacts.md](automated-test-artifacts.md)).

- Fresh opencode session per cycle; every prompt embeds the exact SSH and inject-submit commands (agents failed to re-derive them), and each blue workdir ships its helper scripts.
- Blue sessions launch sequentially — concurrent opencode boots race its DB.
- One scrim per resolved range workspace: a flock under `~/.tezcatlipoca/locks/` (`scrim/lock_ops.py`) refuses a second scrim, naming the holder. The deploy's engine lock covers engine-touching work only; a `--skip-deploy` scrim takes none of it.
- Red dials a public LLM endpoint directly from red01; the reverse tunnel exists for operator-side endpoints (local llama.cpp, a tailnet LLM host) only.

### Staging

`RUNTIME_FILES` (`teams.json`, `estate-state.json`, `credentials.txt`, `nakon-config.json`, `packet.md`, `event.conf`) are regenerated per deploy and never copied into a new competition; the skip/re-copy set is `scrim/staging.py` (`stage_author`, `creds_from_files`, `stage_deploy`) — all secrets come from the state document, `teams.json` and `credentials.txt`. Deploy success means the final-phase marker in stdout or state; `deploy.log` carries full stdout+stderr because the console tail is too short. The `stage_verify` fire test (`scrim/fire_test.py`): stop nginx on team1 web01, require DOWN on the scoreboard **and** a dead HTTP probe from the engine's path (the scoreboard alone can lie), restore, re-prove; any miss aborts before T0 with the manual fix — failing closed is cheap. Other verify failures stay warnings.

### Quotient sessions

Quotient's one-session-per-account eviction has not reproduced on a live engine; the harness shares one cookie jar per account and never logs in ad hoc anyway (`./qlogin` only after `{"error":"Forbidden"}`) — simpler, and converging either way (`scrim/quotient_api.py`). The services API keys on the engine's team **IDs** (1, 2, …), not the subnet identifier (101, 102); the latter returns `Forbidden` even with a valid cookie (`_team_tid`).

### Blue agents

- opencode's base prompt is large: the advertised context must leave room under the endpoint's real capacity or the session self-compacts fatally. Per-endpoint budgets: `scrim/blue_agent.py`; `--reasoning-effort` is cloud-only (`effort_json`).
- `api_key` (`scrim/llm_endpoints.py`): a public endpoint requires its own key env (`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, …) and raises when missing; an operator-side endpoint gets `OPENROUTER_API_KEY` when set, else the literal `local`. Red's key is pushed separately to red01 as `TLALOC_LLM_API_KEY` in `/etc/tlaloc/env`.
- Launch opencode only as `bash -c 'exec opencode run …'` in its own process group with `stdin=DEVNULL`: exec'd straight from Python its server dies silently (the shell parent is the working differentiator), and `subprocess.run(timeout=…)` cannot abort it — the server grandchild holds the pipes and the shared lock — so the timeout path `os.killpg()`s. Per-team `HOME`/XDG keep the state DB and logs in the run dir; failed cycles append the server-log tail.
- `blue_feed_loop` is serialized per **endpoint** (`stage_run` builds one lock per distinct base URL) and staggers starts; both are needed because the shared local endpoint has few slots and rejects oversized prompts. Only team2 has its own endpoint flags (`--blue2-base-url`/`--blue2-model`); every other team shares the default — decide the split before launch. Pacing and per-cycle deadline: `CYCLE_TARGET_PERIOD`/`CYCLE_TIMEOUT` (`scrim/core.py`; `TEZ_CYCLE_TIMEOUT` overrides for a slower endpoint). Each cycle keeps its prompt and transcript under `cycles/`.
- `finalize_blue_narratives` runs before capture: a team whose cycles timed out, crashed or never started has no REPORT.md, so the harness files its own record, labelled harness-synthesized; the collector's "present agent, missing canonical document" warning stays as the guard for a run that died before this point.
- Cycle prompt (`scrim/blue_prompt.py`): notebook first, then at most two change/fix actions (read-only investigation unbounded — hunts are where blue learns); priority restore > near-due inject > one hunt item, then stop. ROE in every prompt: never attack the engine (`$ENGINE_IP`); never change the scoring-check accounts.
- `reanchor_injects` (`scrim/inject_sync.py`): inject offsets resolve at deploy time, so a `--skip-deploy` restart or long staging gap would reach T0 with injects expired; right after t0 they are re-resolved at now and pushed via UpdateInject (`POST /api/injects/{id}`), matched by title, re-listing every attachment under keep-files (UpdateInject deletes unlisted ones). Unconditional and idempotent; unordered offsets abort before the event.

### Monitoring and clock

- `monitor_loop` snapshots the scoreboard from T+0; `scoreboard-state.jsonl` is the structured source of truth, `monitor.log` human-readable. Red's `events.jsonl` and newest report are pulled every snapshot so a teardown-time scp flake can't lose them.
- t0 is captured **after** `stage_red` returns: red setup can take tens of minutes and mustn't count against `--duration-min`; the T0 log line prints the out-of-scoring setup time.
- Red starts its loop **at T0**, after the seed and pre-T0 verify: the seed owns the
  range, but an opening takedown before T0 would make blue start against an outage it
  never saw. `scrim/cli.py` starts `red_link.start_red_director()` after the T0 marker.
- The window is authoritative: `supervise_workers` stops at the deadline plus `EVENT_END_GRACE` and `_opencode_run` kills the session as soon as `stop` is set — capture and teardown run at the deadline even when a cycle would have overrun it (`scrim/supervisor.py`).

### Red

`stage_red` refuses T0 unless, in order: `verify_tlaloc_seeded` finds the pulled manifest readable with a stage on at least one box; `check_red_llm` proves red01 itself can dial the endpoint; `verify_red_reaches_teams` sees a reachable box on every team — judged from the `red_identity`/`red_teams` summary lines, not verify's exit code (the full run's rc also carries unrelated estate gates).

- Posture: the competition's `red.*` block (Compfile keys or its `red_yaml` file) supplies the decision model; anything omitted is tlaloc's default (`tlaloc/tlaloc/config.py`). The scrim records its own pacing caps (`red_stage.red_pacing_caps`) in the run manifest, and the report judges `max_simultaneous_down` against the run's end cap, not a fixed threshold.
- Red01's slot resolution and refusals are the deploy's: `--red-vmid`/`--red-ip`, else Compfile, else recorded placement, else derived — occupied vmid refused unless this run owns it, an IP answered by a running guest refused, an unverifiable address refused unless already recorded or Compfile-set ([environment-facts.md § vmid occupancy](environment-facts.md#vmid-occupancy)).
- **LLM gate**: operator-side reachability proves nothing about what red01 can reach. `--red-tunnel` (`auto` = on for operator-side endpoints, ignored for a public API) runs a reverse SSH forward with keepalive and supervisor restart so red01 dials a tunnel-local URL (`scrim/red_tunnel.py`); `check_red_llm` curls `/models` from red01 (direct, then via the engine jump) — any HTTP answer from the endpoint passes (the probe is deliberately keyless), anything else aborts before T0. `red_llm_watch` repeats it every tick with hysteresis (`scrim/llm_probe.py`): one blip logs, `RED_LLM_FAIL_THRESHOLD` consecutive failures restart the tunnel once and write a durable alert; recovery clears the episode.
- The day-0 seed runs `tlaloc seed` on red01 from `/opt/tlaloc`; the agent writes a per-box manifest at `/var/lib/tlaloc/intel/red/manifest.json` and the deploy pulls it to `<comp>/red/manifest.json`. All red state lives on red01 under `/var/lib/tlaloc` (`world.json`, `events.jsonl`, reports, `intel/`); there is no operator-side red state dir.
- Identity: red01 keeps its red-segment source (`common/constants.RED_SEGMENT_IP`) to the engine-node teams; a satellite jump SNATs red to the team gateway. `verify-competition.py --red-identity --red-teams all` is the third pre-T0 gate. There are no red mode/subnet/template/storage flags — the private bridge, the segment and the engine rules belong to the deploy.
- `pull_red_state` (`scrim/red_link.py`) waits `RED_REPORT_GRACE_SEC` for red's `report.md`, stages root-owned artifacts through `/tmp/ba`, and scps into `run_dir/evidence/red/` — direct first, then via the engine jump; `pull_red_snapshot` is the never-raises in-run variant. `_red_ssh_ctx` takes the address from `--red-ip` or this competition's recorded identity — never a sibling checkout's config — and the same `_red_ssh_spec` feeds `record_red_agent`, so harness and teardown collector dial one address.
- Liveness has an out-of-band signal: `red_box_power_state` asks Proxmox (no guest networking) and `red_liveness` prints on every failed guest-path probe — "red is dead" and "the path to red is dead" read differently in the run log.

### Capture and teardown

- `stage_capture`: re-logins on `Forbidden`, falls back to the team account if admin is locked out, best-effort pauses scoring, and archives scoreboard state plus the blue workdirs, submissions and cycles. It dumps `evidence/final-scoreboard.json` **before** pausing — teardown destroys the scoring DB and the report's "Final scores" reads that dump.
- `stage_teardown`: stops the LLM tunnel, pulls red's state, runs the artifact collector + `INTERACTION.md` so the report grades the final world; `teardown_red` captures `tlaloc report`, pulls the whole state dir into `<comp>/red/`, stops `tlaloc.service` and destroys red01 through the run's ownership tags, failing if the vmid survives. The same terminal teardown the `destroy-competition.py` CLI runs executes in-process unless `--keep-range`.

### Mid-run box resets

Reset a dead box from a second shell while the harness runs:

```bash
python3 redeploy-competition.py --competition <id> --teams N --boxes web01 --mode reset --yes
```

The engine lock it takes serializes only other engine-touching operations. The box's services go DOWN during the reset and blue's watchdog may flag it (expected). Replant/rebuild rungs retake `tz-ready`. `--reset-event` composes with `reset` for full reruns.

## scrim-report.py

Reads a run dir (`evidence/red/`, `blue-team*/`, `scoreboard-state.jsonl` if present) and writes `INTERACTION.md`. The harness runs it at teardown before red01 is destroyed; when the harness never got there, the artifact step runs it (`artifacts_lib/verdict.ensure_verdict`), `artifacts_ops.ingest_verdict` folds the verdict and gate table into `test.json`, and `REPORT.md` quotes it. Score composition and why a run scoring 0 is a failed run: [rehearsal-gates.md](rehearsal-gates.md).

- Event timestamps are red01's clock, treated as UTC. `load_red_events` merges the final `events.jsonl` with in-run snapshots, filters to a window around T0 and dedupes (`scrim_report/loaders.py`); T0 is `run.json`'s `t0` (the authoritative event start), else `world.json` `meta.event_start` under `evidence/red/`.
- `load_scoreboard` returns `[]` without `scoreboard-state.jsonl`; the report then runs in no-scoreboard mode — down-minutes, TTR and max-simultaneous-down are n/a, restorations inferred from re-kills and marked as such.
- `host_label` maps the team subnet's third octet to the team; box names come from the competition's `boxes.json`, and anything unresolvable degrades to the raw octet rather than inventing a name from another lineup (`scrim_report/host_labels.py`). `windows_footholds` comes from the footholds' `windows` flag in `world.json`, else from initial-access events against those boxes' octets.
- Evictions are not a red gate — red's health checks only see SSH-class footholds, so a red-observed zero is an observability limit, not a failed red (the reason rides the `GATES` comment); blue's self-reported `EVICTED:` lines count separately (`evictions_logged`) and are never red evidence. The interaction score still adds red-observed evictions.
- Metric definitions — stall windows, `down_windows` (a service still down at the last snapshot closes at event end and counts), `blue_metrics` (cycle rc from `feed.log` headers, notebook entries, eradication = persistence indicator AND removal verb, `sub*` inject deliverables) — are `scrim_report/red_side.py` and `blue_side.py`.

## red/tlaloc_ops.py

The one red deployment path: phase 7 calls `plant_assume_breach(ctx)` and `scrim/red_stage.stage_red` calls the same function, so a scrim cannot stage a red the deploy would not.

- Reads the competition's `red.*` block (Compfile keys or its `red_yaml` file), validates it, and renders `.tlaloc-config.yaml`; the file pushed to `/etc/tlaloc/config.yaml` is that block over tlaloc's defaults, and `verify_config_contract` re-reads the pushed file and refuses any field the deploy meant to set that it dropped.
- Deploys red01 through `red/red01_ops.py`: clone or adopt this run's VM (ownership tags), pin both NICs, grow the disk to `red.disk_gb` (`DEFAULT_RED_DISK_GB` in `red/tlaloc_ops.py`), install the agent's apt dependencies.
- C2: Sliver is the default (`red.c2_engine sliver`; `red.c2 off` opts out — C2 box is the Compfile's `red.c2_ip`, else red01): `red/sliver_ops.py` installs the pinned server and operator profile, `tlaloc c2-prep` runs on red01 to build the full payload matrix (every transport, plus beacons/pivots), and the engine gets the red-segment rules plus one DNAT per transport port (re-asserted by `range-red-firewall.timer`). `red.c2_implant_spread full` plants every network transport on every box. A failed C2 or payload step aborts the deploy. Mythic remains selectable with `red.c2_engine mythic`.
- Stages `tlaloc/` at `/opt/tlaloc` (staged, not installed: the unit is what puts it on the import path), pushes the competition's intel files, the config and `/etc/tlaloc/env` (the LLM key), installs `tlaloc.service`, runs the day-0 seed, and pulls the seed manifest to `<comp>/red/manifest.json`.
- Verdict: `tlaloc report` writes `report.md` and `restore-table.json` on the box; `pull_evidence` tars `/var/lib/tlaloc` into `<comp>/red/` and writes `verdict.json` (summary, entry and command counts). `RED-TEAM.md` is filed from `report.md`.
