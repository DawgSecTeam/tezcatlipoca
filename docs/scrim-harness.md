# Scrim harness

Design notes for `run-agent-scrim.py` (lifecycle orchestrator), `scrim-report.py` (interaction scoring) and `red/beacon_ops.py` (hunt-artifact planting). Numeric pass/fail gates: [rehearsal-gates.md](rehearsal-gates.md).

## Module map

The two scripts are thin CLIs over the `scrim/` and `scrim_report/` packages. Cross-module calls are module-qualified (`procs.run_tree`, `quotient_api.qget`), so tests patch where a function is *defined* (`mock.patch.object(procs, "run", ...)`). `log` is from-imported; patch it in the module under test.

| `scrim/` module | Responsibility |
|---|---|
| `cli`, `core`, `runfiles`, `run_manifest` | Flags and stage sequence; repo paths, timing constants, shared helpers; run-dir file names shared with the report; `run.json` and resume rules |
| `procs`, `supervisor` | Process-group supervision (`ScrimTimeout`); worker-thread supervision |
| `quotient_api`, `staging`, `compworld`, `fire_test` | Cookie jars and locks, `qget`/`_qlogin`; `stage_author`/`creds_from_files`/`stage_deploy`; per-comp facts and `verify_cmd`; `stage_verify` + web01 fire test |
| `blue_helpers`, `endpoints`, `blue_prompt`, `inject_sync`, `blue_agent`, `blue_watchdog` | Generated blue workdir files (`mybox`, `qlogin`, `score.py`, `myscore`, `submit-inject`, opencode config, notebook); `api_key`/`blue_ep`; cycle prompt and `scoreboard_delta`; `reanchor_injects`/`inject_brief`; `stage_blues`/cycle runner/`blue_feed_loop`; watchdog |
| `scoreboard_monitor`, `red_link`, `red_tunnel`, `llm_probe`, `red_stage` | `monitor_loop`; red01 ssh and snapshot pull; `RedTunnel`; red-LLM hysteresis; `stage_red` |
| `event_run`, `evidence`, `teardown_stage`, `test_folder` | `stage_run`; capture; `stage_teardown`; run test folder and report |

`scrim_report/`: `cli`, `loaders`, `host_labels`, `timefmt`, `red_side`, `blue_side`, `gate_table`, `render`.

## run-agent-scrim.py

Stages: author (optional) → deploy → verify + fire test → blue agents (opencode, fresh session per cycle) → red (bad-auto at `../bad-auto`) → feeds and monitoring to the deadline → evidence capture → teardown (red01 + range, unless `--keep-range`). The run dir defaults to `competitions/<comp>/.automated-tests/<run-id>/` (`--run-dir` overrides; recorded in `test.json.paths.run_dir`). Before red01 is destroyed the harness collects `RED-TEAM.md`, `BLUE-TEAM.md`, `REPORT.md`, `evidence/**` and folds scrim-report's verdict into `test.json` ([automated-test-artifacts.md](automated-test-artifacts.md)).

Design rules:

- Fresh opencode session per cycle with a compact prompt; exact SSH and inject-submit commands embedded in every prompt (agents failed to re-derive them); helper scripts materialized into each blue workdir.
- Blue sessions launch sequentially (concurrent opencode boots race its DB).
- Red reaches OpenRouter directly from red01; reverse tunnels are for tailnet-local endpoints only.

### Staging

- `RUNTIME_FILES` (`teams.json`, `.deploy_state.json`, `credentials.txt`, `nakon-config.json`, `packet.md`, `event.conf`) are regenerated per deploy and never copied into a new competition. `stage_author` also skips `.postclone-swept`, `LOG.md`, `sub-*`, `.nakon-domain-*`; re-copies `injects/`; rewrites the template's `Compfile` first line.
- `creds_from_files`: all secrets come from `.deploy_state.json`, `teams.json`, `credentials.txt`.
- `stage_deploy` writes full stdout to `deploy.log` on failure (the console tail is too short). Success requires `last_phase: 8`/`Deploy complete` in stdout or state.
- `stage_verify` fire test: stop nginx on team1 web01, wait 150 s, expect team1 DOWN on the scoreboard **and** a dead HTTP probe from the engine's path (the scoreboard alone can lie). Restore with unmask + start, up to 3 attempts, then require scoreboard-up and a live probe. Any miss aborts with the manual fix; failing closed before T0 is cheap. Other verify failures stay warnings.

### Quotient sessions

> Questionable: the whole design assumes Quotient allows one session per account; [known-issues.md](known-issues.md) records that this was not reproduced.

- Every login kills the account's previous cookie, so nobody logs in ad hoc. `./qlogin` runs only after `{"error":"Forbidden"}`. All clients share one jar per account (blues/monitor `/tmp/jar.team<n>`; capture `evidence/.jar-admin`); the newest login wins, so concurrent refreshes converge without locking.
- `qget` re-logins into the shared jar and retries once on an error. `score.py` reads the Netscape jar (session cookie on the last non-comment line) and shells out to `./qlogin`; `submit-inject` resubmits after one `./qlogin`.
- `_team_tid`: the services API keys on the engine's IDs (1, 2, …), not the subnet identifier (101, 102); the latter returns `Forbidden` even with a valid cookie.
- `parsed_status` raises so callers choose; `status_text` renders a self-diagnosing line (`scoreboard unreachable (TypeName: msg)`).

### Blue agents

- `stage_blues`: opencode's base prompt is ~20k tokens, so the advertised context must leave room or it self-compacts fatally. Local endpoints: context 60000, output 4000, no `reasoning_effort`. Cloud: 120000/16000 plus `--reasoning-effort` (`effort_json`).
- `api_key`: `OPENROUTER_API_KEY`, else `BAuto_LLM_API_KEY` from `bad-auto/.env`; local endpoints get the literal `local`.
- `_opencode_run` launch hardening: `stdin=DEVNULL`, own process group, per-team `HOME`/XDG under `blue-team<n>/.opencode-home/` (state DB and logs land in the run dir). The child is `bash -c 'exec opencode run …'` with the prompt in `$CYCLE_PROMPT`, because opencode's server dies silently when exec'd straight from Python (the shell parent is the working differentiator). It is a `Popen` whose timeout path `os.killpg()`s the session: `subprocess.run(timeout=…)` hung ~80 min because opencode's server grandchild held the pipes and the shared lock. `_opencode_log_tail` appends the server-log tail to failed cycles.
- `blue_feed_loop`: serialized per **endpoint** (`stage_run` builds one lock per distinct base URL). Only team2 has its own endpoint flag (`--blue2-base-url`/`--blue2-model`); every other team shares the default, so decide the split before launch. Throughput: at 8 teams each team gets a cycle about every 10 minutes, the biggest lever on blue's score. One immediate retry per failed cycle. Each cycle keeps its prompt and transcript (`cycles/cycle-T+NNN.{prompt.txt,output.log}`); `feed.log` is a 2000-char tail.
- Pacing: sleep = clamp(30..600 s, `CYCLE_TARGET_PERIOD` 600 − actual cycle time); `CYCLE_TIMEOUT` 1800 s per run.
- `stage_run`: blues staggered (team1 T+0, team2 +300 s) **and** lock-serialized; both are needed because the shared local endpoint has few slots and rejects oversized prompts.
- `blue_cycle_prompt`: notebook first, then at most two change/fix actions (read-only investigation is unbounded, since hunts are where blue learns), then stop. Priority: restore > inject due within 30 min > one hunt-checklist item. States a ~25-minute budget with wrap-up by minute 20. ROE in every prompt: never attack the engine (`$ENGINE_IP`); never change the scoring-check accounts (triage/svc-imaging/wardops).
- `scoreboard_delta`: new DOWN → "RESTORE IT FIRST"; back UP → "expect red to re-attack it"; still DOWN annotated with since-T+.
- `inject_brief`/`due_in_min`: unsubmitted and due ≤30 min is flagged "TASK #1". The engine's timezone isn't guaranteed, so either UTC or local interpretation is accepted if it lands within −15 min … +12 h.
- `NOTEBOOK_TEMPLATE`: SNAPSHOT (current state + next action) / OPEN INCIDENTS / HUNT CHECKLIST / DONE. Embedded in each prompt (2500 chars) with the last 5 `LOG.md` lines; the update is cycle step 0. `NOTEBOOK.md` persists across reruns; `LOG.md` restarts.
- `MYBOX`: Linux over key auth, Windows over password (sshpass), through the engine ProxyCommand.

### Monitoring and clock

- `monitor_loop` snapshots every `MONITOR_INTERVAL` (300 s) from T+0 (down-minute math needs the full window). `scoreboard-state.jsonl` is the structured source of truth; `monitor.log` is human-readable. Red's `events.jsonl` is pulled every snapshot so a teardown-time scp flake can't lose it.
- t0 is captured **after** `stage_red` returns: red setup can take 5–30 min and mustn't count against `--duration-min`. The T0 log line prints the out-of-scoring setup time.
- `reanchor_injects`: inject offsets resolve at deploy time, so a `--skip-deploy` restart or long staging gap reaches T0 with injects expired. Right after t0 the offsets are re-resolved at now and pushed via UpdateInject (`POST /api/injects/{id}`), matched by title, re-listing every attachment under keep-files (unlisted ones are deleted). Unconditional and idempotent; unordered offsets abort before the event.

### Red

- Posture: `decision_window_min` 2; concurrent-down ramp 2/4/6 (6 at T−15); `min_standing_services=2` floor. Re-tune after a rehearsal, not before.
- Red01 wiring is flag-driven: `--red-ip/--red-gw/--red-storage` default to 10.0.0.198 / 10.0.0.1 / `hdd`; `--red-vmid/--red-template` override the reservation. Without `--red-vmid` it resolves from Compfile `red01_vmid`, the comp's bad-auto config, or `<scoring-vmid>+170`, never bad-auto's shared default 999; preflight refuses an occupied slot. Local endpoints get `timeout: 120`, `json_retries: 0` (cloud: 240/1); a single 8–16 min decision otherwise yields ~2 actions/hour.
- **LLM gate**: `validate-llm` and the dry-run run on the operator and prove nothing about what red01 can reach. `--red-tunnel` (default `auto`, on for non-openrouter endpoints) runs a reverse SSH forward (red01 dials `localhost:8180`; `ServerAliveInterval` + 15 s supervisor restart). Red01's config gets the localhost URL only after the operator-side check used the real one. Post-deploy, `check_red_llm` curls `/models` from red01 (direct, then via the engine jump); non-200 aborts before T0. `monitor_loop` repeats that probe every tick (`red_llm_watch`) and alerts once per failure episode.
- State dir: operator-side bad-auto needs a writable one; `BAuto_STATE_DIR` under the run dir affects the host side only (the VM's config hardcodes `/var/lib/bad-auto`).
- Identity: `--red-mode/--red-subnet/--red-seg-ip` pass through to bad-auto's `deploy.red_mode` ([architecture.md](architecture.md#network-and-isolation)); verify with `verify-competition.py --red-identity`.
- `pull_red_evidence`: `events.jsonl`, `world.json`, and the bad-auto journal (`sudo -n journalctl -u bad-auto`) are fetched before `badauto destroy`. If the direct scp hangs (>60 s), fall back to the engine as a jump host. `pull_red_snapshot` is the in-run best-effort variant (75 s timeout, never raises).
- `_red_ssh_ctx`: red01 IP defaults to 10.0.0.198, overridden by the competition's own bad-auto config (`deploy.red_ip`); never the sibling checkout's shared config.

### Capture and teardown

- `stage_capture`: re-logins on `Forbidden`, falls back to the team account if admin is locked out, best-effort pauses scoring, freezes `scoreboard-state.jsonl` into evidence, archives blue LOG/NOTEBOOK/feed.log, `sub-*` deliverables, `submissions/`, `cycles/`. It dumps `evidence/final-scoreboard.json` (teams, injects, per-team service states) **before** pausing, because teardown destroys the scoring DB and the report's "Final scores" reads that dump.
- `stage_teardown`: red evidence first, then `badauto destroy` (red01 + engine firewall rules of both modes, best-effort), then `destroy-competition.py` unless `--keep-range`.

### Mid-run box resets

Reset a dead box from a second shell while the harness runs:

```bash
python3 redeploy-competition.py --competition <id> --teams N --boxes web01 --mode reset --yes
```

The engine lock it takes serializes only other engine-touching operations. The box's services go DOWN during the reset and blue's watchdog may flag it (expected). Replant/rebuild rungs retake `tz-ready`. `--reset-event` composes with `reset` for full reruns.

## scrim-report.py

Reads a run dir (`evidence/red/events*.jsonl`, `world.json`, `blue-team*/{feed.log, LOG.md, NOTEBOOK.md, REPORT.md, submissions/, sub*.md}`, `scoreboard-state.jsonl` if present), writes `INTERACTION.md`. Runs at teardown (best-effort), or from teardown if the harness never got there; `artifacts_ops.ingest_verdict` folds the verdict and gate table into `test.json`, and `REPORT.md` quotes it.

- **Score** = restorations + red restore-reactions + evictions + injects + eradication. A run scoring 0 is a failed run regardless of red's log: "parallel monologues. Red and blue never touched the same game."
- Constants: `STALL_SEC = 600` (stall = ≥10 min without a new successful action); `REAKILL_GAP_SEC = 900` (re-attack after ≥15 min implies a restore); `RESTORE_TTR_GATE_MIN = 15`. `GATES` is the machine source for [rehearsal-gates.md](rehearsal-gates.md).
- `parse_ts`: event timestamps are red01's clock, treated as UTC.
- `load_red_events` merges the final `events.jsonl` with in-run snapshots, filtered to [T0 − 120 s, T0 + 8 h] and deduplicated (ts/kind/tactic/target/detail). T0 is `world.json` `meta.event_start` (evidence or `bad-auto-state/`).
- `load_scoreboard` returns `[]` without `scoreboard-state.jsonl`; the report then runs in no-scoreboard mode (down-minutes, TTR, max-simultaneous-down are n/a; restorations inferred from re-kills and marked).
- `host_label` maps any third octet 101–199 to team X−100; last octet 2=dc01, 3=win01, 4=web01, 5=app01, 6=db01.
- `takedown_fields` prefers structured `data` (ip/unit/service/mode), falls back to regexing the detail string; mode defaults to "firewall" for `impact_firewall`, else "stop". `foothold_list` accepts dict-by-ip or list.
- Red stalls are gaps between successful non-`health_check` actions, and the opening (T0 → first success) counts. With no clock (no `world.json`/`event_start`) stall math is skipped rather than aborting the report (the `timeouts` count feeds a gate).
- `windows_footholds` comes from `world.json`'s `windows` flag, else is inferred from initial-access events against Windows boxes' last octets in `boxes.json`.
- Evictions are counted as 0 (not yet observable); rendered "n/a".
- `down_windows` per team: real DOWN→UP transitions between snapshots, TTRs, down-minutes, max simultaneous down; a service down at the last snapshot closes at event end and counts.
- `blue_metrics`: cycles rc=0 from `feed.log` `===== cycle ... rc=N =====` headers ("MANUAL" counted separately); notebook entries = non-heading `LOG.md` lines + checked `NOTEBOOK.md` boxes; eradication = log lines matching beacon/backdoor indicators (tznet, svc-netupdate, TzNet, red_key, authorized_keys, backdoor, rogue, uid 0, unauthorized) AND a removal verb (removed/deleted/disabled/uninstalled/locked/changed-back/reset); injects = `sub-*.md`/`sub-*.txt` + `submissions/`.

## red/beacon_ops.py

Plants raw-socket beacons on team Linux boxes as blue hunt artifacts in phase 7 when Compfile has `team_beacons 1`, before the `tz-ready` snapshot so clones and restore points inherit them.

- Each beacon is a compiled `artifacts/rawsockets-beacon/beacon.c` run by `wda-digest.service` ("Wardline Data Digest Agent", `Restart=always`, `RestartSec=20`), sending forged SYN packets with a BEA1 payload to the team gateway `192.168.<tid>.1:4444`. Not scored, not destructive; blue must find the process/unit/binary and its periodic egress and disable it properly (stop alone won't stick).
- `BEACON_INTERVALS` vary per box (web01 45 s, app01 60 s, db01 90 s) so the periodicity isn't uniform.
- `_build_beacon`: static build on the operator host if possible; a dynamic local build won't run on the Ubuntu boxes, so failure means on-box compilation (the install script apt-installs gcc if needed).
- `_ssh_box` with `sudo_password` runs as root via `sudo -S bash -s`, because the planted writable-sudoers misconfig breaks passwordless sudo on some boxes. Ship the install script as a file (`/tmp/.wda-install.sh`) and run `sudo -S bash <file>`: sudo consumes the stdin password only when prompted, so a script on stdin would execute the password line as a command under NOPASSWD.
- `plant_team_beacons` warns and continues per box (scenario flavor must never abort a deploy). Success = `systemctl is-active` prints `active`. Artifacts: `/usr/local/lib/.sysmon/beacon`, the unit file, `/etc/.sysmon.conf` (`C2=<gw>:4444`, `agent=wardline-<box>`). All box access goes through the engine as ProxyCommand.
