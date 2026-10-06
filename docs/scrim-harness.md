# Scrim harness — design notes & lessons

Design notes, rationale, and hard-won lessons for the agent-scrim harness
scripts: `run-agent-scrim.py` (whole-lifecycle orchestrator),
`scrim-report.py` (interaction scoring), and `beacon_ops.py` (hunt-artifact
planting). This content used to live only in comments and docstrings inside
those files; it was extracted here on 2026-09-21. **An earlier commit (`af7468c`) stripped the
docstrings and comments those notes came from, so for most of the symbols below this file is now
the only record of the design decision — treat a deletion here as deleting the rationale, not
duplicating it.** It deliberately does NOT
duplicate the sibling docs: `docs/rehearsal-gates.md` owns the numeric
pass/fail gates, `docs/reports/harness-upgrades-plan.md` owns the rev-2 upgrade
record (what changed, where it landed, what's left), and
`docs/reports/dress-rehearsal-prompt.md` owns the rehearsal runbook.

**Contents:** [run-agent-scrim.py](#run-agent-scrimpy) · [scrim-report.py](#scrim-reportpy) ·
[beacon_ops.py](#beacon_opspy)

## Module map (release 0.2.0 split)

`run-agent-scrim.py` and `scrim-report.py` stay the CLI entrypoints (identical flags) but are now
thin wrappers; the code lives in two packages, one responsibility per module. Every symbol named in
the sections below still exists under the same name, in the module listed here. Cross-module calls
are module-qualified (`procs.run_tree`, `quotient_api.qget`, `core.REPO`), so each function has one
binding and tests patch it where it is *defined* (`mock.patch.object(procs, "run", ...)`), not
where it is used. `log` is the one exception: it is from-imported, so patch it in the module under
test.

`scrim/` (the harness):

| Module | Responsibility |
|---|---|
| `cli.py` | `main()`: flags, resume handling, the stage sequence |
| `core.py` | repo paths (`REPO`, `BAD_AUTO`), timing constants, `log`, evidence writers, and the shared helpers (`vm_username`, `engine_ip_from`, `engine_proxy`, `box_ssh_base`, `read_comp_json`, `is_local_endpoint`, `is_windows_box`, `now_iso`) |
| `runfiles.py` | run-dir file names shared with the report (`run.json`, `alerts.jsonl`, `scoreboard-state.jsonl`, `final-scoreboard.json`) |
| `procs.py` | `run_tree`/`run` (process-group supervision, `ScrimTimeout`), `box_sudo_stdin` |
| `quotient_api.py` | cookie jars + per-account locks, `qget`/`_qlogin`, `parsed_status`, `services_to_rows` |
| `staging.py` | `stage_author`, `creds_from_files`, `stage_deploy` |
| `compworld.py` | per-comp facts: `comp_world`, `_web01_units`, `_verify_flags`, `verify_cmd`, the unit/display maps |
| `fire_test.py` | `stage_verify` (verify-competition + the web01 stop/restore fire test) |
| `blue_helpers.py` | generated blue workdir files: `mybox`, `scorch`, `qlogin`, `score.py`, `myscore`, `submit-inject`, opencode config, notebook template |
| `endpoints.py` | `api_key`, `provider_key`, `effort_json`, `blue_ep` |
| `blue_prompt.py` | `blue_cycle_prompt`, `scoreboard_delta` |
| `inject_sync.py` | `reanchor_injects`, `_inject_reanchor_plan`, `inject_brief` |
| `blue_agent.py` | `stage_blues`, the opencode cycle runner, `blue_feed_loop`, `run_cycle_with_retry` |
| `blue_watchdog.py` | `watchdog_script`, `blue_watchdog_loop` |
| `scoreboard_monitor.py` | `monitor_loop` (scoreboard snapshots, red pull, red-LLM probe tick) |
| `red_link.py` | red01 ssh addressing (`_red_ssh_spec`/`_red_ssh_ctx`), `pull_red_snapshot`, `check_red_llm`, `red_llm_url` |
| `red_tunnel.py` | `RedTunnel`, `maybe_start_red_tunnel` |
| `llm_probe.py` | `red_llm_watch` hysteresis, `restart_red_llm_tunnel`, run-dir alert journal |
| `red_stage.py` | `stage_red`, `verify_red_reaches_teams` |
| `supervisor.py` | worker-thread supervision (`WorkerDiedError`, `supervise_workers`, `join_workers`) |
| `event_run.py` | `stage_run`, `run_event_and_finish` |
| `evidence.py` | `stage_capture`, `capture_engine_evidence`, `capture_blue_evidence` |
| `teardown_stage.py` | `stage_teardown`, `teardown_red` (+ the vmid-gone assertion) |
| `test_folder.py` | the run's test folder: `resolve_run_dir`, `record_*_agent`, `collect_run_artifacts`, `write_interaction_report` |
| `run_manifest.py` | `run.json` + resume rules (`record_phase`, `resume_refusal`, ...) |

`scrim_report/` (the interaction report): `cli.py` (flags), `loaders.py` (run-dir readers),
`host_labels.py` (box labels, Windows octets), `timefmt.py`, `red_side.py` (red metrics),
`blue_side.py` (blue metrics, down windows, inject counting), `gate_table.py` (rehearsal gates,
components, pinned self-test), `render.py` (`build_report`).

`beacon_ops.py`, `run-schedule.py`, `observe-soak.py` and `generate-packet.py` are single-purpose
scripts and were not split.

## run-agent-scrim.py

Deploys, verifies, and runs an agent-manned scrim end to end: author
(optional) → deploy → verify + fire test → blue agents (opencode, fresh
session per cycle) → red agent (bad-auto, sibling checkout at `../bad-auto`)
→ scheduled feeds and monitoring until the deadline → evidence capture →
teardown (red01 + range, unless `--keep-range`). Run dirs default to the run's
test-artifact folder, `competitions/<comp>/.automated-tests/<run-id>/`, so the whole run's
evidence lands next to the competition it tested; `--run-dir` still overrides the location
(the folder is then recorded in `test.json.paths.run_dir` and the collector copies from it).
Before teardown destroys red01, the harness collects that folder into the standard shape
(`RED-TEAM.md`, `BLUE-TEAM.md`, `REPORT.md`, `evidence/**`) and folds scrim-report.py's verdict
into `test.json` — see [automated-test-artifacts.md](automated-test-artifacts.md).

- Module docstring — "bake-in lessons" from the agent-scrim-2026-09-16 debrief:
  - fresh opencode session per cycle with a compact prompt (survives
    small-context local models, works even better on cloud models)
  - exact SSH/inject-submit commands EMBEDDED in every cycle prompt (blue
    agents failed to re-derive them from the briefing)
  - helper scripts (`mybox`/`myscore`/`submit-inject`, plus `qlogin`/`score.py`)
    materialized into each blue workdir
  - blue sessions launched SEQUENTIALLY (concurrent opencode boots race its DB)
  - red via OpenRouter direct from red01 (internet) — reverse tunnels are only
    needed for tailnet-local endpoints
  - teardown of the whole range at the end unless `--keep-range`
- `RUNTIME_FILES` — regenerated per deploy; never copied into a fresh
  competition (`teams.json`, `.deploy_state.json`, `credentials.txt`,
  `nakon-config.json`, `packet.md`, `event.conf`).
- `stage_author` — the pipeline's sweep marker **`.postclone-swept`** is a resume marker and must
  never leak into a fresh competition; `stage_author` skips that name (fixed 2026-10-01 — it used
  to skip the retired `.phase6-swept`, so the real marker was copied into every competition
  authored from a swept template).
  `RUNTIME_FILES`, `LOG.md`, `sub-*`, `.nakon-domain-*` are also skipped, and `injects/` is
  re-copied deliberately. The template's `Compfile` first line is rewritten to the new name.
- `creds_from_files` — all secrets/logins come from the post-deploy artifacts
  (`.deploy_state.json`, `teams.json`, `credentials.txt`) — no terraform
  output needed.
- `stage_deploy` — keep the WHOLE pipeline output on failure: the tail-15
  printed to the console is not enough to diagnose a mid-phase failure (e.g.
  the real terraform error), so full stdout is written to `deploy.log` in the
  run dir. Success requires `last_phase: 7` / `Deploy complete` in stdout or
  `last_phase == 7` in `.deploy_state.json`; resume with `--from-phase`.
- `stage_verify` — fire test: stop nginx on team1 web01, wait 150 s, expect
  team1 DOWN on the scoreboard AND a dead HTTP probe from the engine's network
  path (the scoreboard alone lies twice: `team_down` swallows exceptions into
  "up", and run-12's stop left the unit dead with nobody checking). Restore:
  unmask + start with up to 3 attempts, then require scoreboard-up AND a live
  probe — `fire test: down_detected=X restored=Y healed=Z`. Any miss ABORTS
  with the manual fix (resume re-validates; failing closed before T0 is
  cheap, discovering it at T0+40 is not). verify failures stay warnings.
  Fixed 2026-09-23 (run-12 left team1's web01-http down at T0).
- `QLOGIN` helper (shipped into each blue workdir) — Quotient allows ONE
  session per account: every login kills that account's previous cookie, so
  NEVER log in ad hoc; run `./qlogin` only when a request returns
  `{"error":"Forbidden"}`.
- `_qlogin` — same rule, orchestrator-side: every client (monitor, status,
  blues, capture) shares ONE jar per account (blues/monitor:
  `/tmp/jar.team<n>`; capture: `evidence/.jar-admin`) and nobody logs in on
  the happy path. The newest login always wins, so concurrent refreshes
  converge on the only valid cookie — no locking needed.
- `qget` — an invalidated cookie comes back as `{"error": ...}`; re-login
  into the shared jar and retry once. The shipped `score.py` follows the same
  discipline (reads the curl Netscape jar — session cookie is on the last
  non-comment line — and shells out to `./qlogin` on rejection; never logs in
  directly). `submit-inject` likewise resubmits after one `./qlogin`.
- `_team_tid` — the services API keys on the ENGINE's own IDs (1, 2, …);
  querying it with the subnet identifier from `teams.json` (101, 102) answers
  `{"error":"Forbidden"}` even with a valid cookie. That was the second half
  of the three-round "scoreboard unreachable" mystery (first half: session
  clobbering).
- `parsed_status` / `status_text` — `parsed_status` raises on failure so
  callers decide whether to render text or record a structured snapshot;
  `status_text` (for the cycle prompt) renders a self-diagnosing failure line
  instead — `scoreboard unreachable (TypeName: msg)` — so the LLM reader can
  see WHY.
- `stage_blues` — llama.cpp context ceiling: opencode's own base prompt is
  ~20k tokens, so the advertised context must leave room for it or opencode
  self-compacts fatally (60000 verified against the slot limit). Local
  endpoints get context 60000 / output 4000 and NO reasoning_effort; cloud
  gets 120000/16000 plus the `--reasoning-effort` fragment (`effort_json`).
- `api_key` — `OPENROUTER_API_KEY` from the environment, else
  `BAuto_LLM_API_KEY` from `bad-auto/.env`; local endpoints get the literal
  string "local" (llama.cpp-style endpoints don't check the key).
- `_opencode_run` — the 17c run died 40/40 with an instant opencode
  "Unexpected server error" from a capture_output thread launch that never
  reproduced in the foreground, so the launch context is hardened instead of
  trusted: no stdin (`stdin=DEVNULL`), own process group
  (`start_new_session=True`), per-team HOME/XDG
  (`blue-team<n>/.opencode-home/`) so opencode's state DB and server logs
  live inside the run dir — triage evidence instead of silent global state
  shared with whatever else runs under this user. Two more lessons from the
  cyberfield E2E: opencode's server dies silently right after "llm runtime
  selected" when the CLI is exec'd straight from python (manual shell runs
  and a pty both worked — the shell parent is the only working
  differentiator), so the child is `bash -c 'exec opencode run …'` with the
  prompt in `$CYCLE_PROMPT` (keeps the multi-KB prompt out of the command
  line); and `subprocess.run(timeout=…)` hung ~80 min past the timeout
  because opencode's server grandchild held the pipes (one hung cycle held
  the shared lock, starving the other team), so the run is a `Popen` whose
  timeout path `os.killpg()`s the whole session before draining.
  `_opencode_log_tail` appends the newest server-log tail to failed-cycle
  output.
- `blue_feed_loop` — serialize the blues behind a per-endpoint lock: the shared
  local endpoint has few slots (and rejects oversized prompts), and even
  cloud runs shouldn't have opencode DBs racing. Serialization is per
  ENDPOINT (`stage_run` builds one lock per distinct base URL): teams sharing
  an endpoint share its lock; a team with its own `--blue{N}-base-url`
  (any N — `--blue2-*`, `--blue3-*`, `--blue4-*` are defined; blue_ep falls
  back to the shared endpoint for anything unset) runs concurrently
  (`blue_ep` resolves the team's endpoint/model; `RUNTIME_FILES` is the
  per-deploy regenerated file set never copied into a fresh competition).
  Before 2026-09-29 the lock/endpoint choice was hardcoded 2-team — teams 3/4
  silently shared team1's endpoint AND lock. One immediate
  retry per
  failed cycle — a transient boot/endpoint failure otherwise costs the whole
  cycle slot to the pacing sleep. Full transcript + exact prompt are kept per
  cycle (`cycles/cycle-T+NNN.prompt.txt` / `.output.log`) for the after-action
  report; `feed.log` stays a short readable tail (last 2000 chars per cycle).
- **How many endpoints a run needs** — the lock serves one team at a time per
  endpoint, in rotation. At 4 teams that is tolerable; at 8 the soak measured each
  team getting a cycle every ~10 minutes of wall time, which halves blue's effective
  attention per team and is the largest single lever on blue's score. **At ≥6 teams,
  spread the blues over ≥2 LLM endpoints** with `--blue2-base-url`/`--blue2-model`
  (`--blue3-*`, `--blue4-*` exist too; each distinct base URL gets its own lock, and
  teams sharing one still serialize). Decide the split before launching — `stage_run`
  builds the lock table from those flags at startup.
- pacing (`blue_feed_loop` tail) — pace off ACTUAL cycle duration: local qwen
  turns are slow, so a fixed pre-cycle sleep both starves throughput and
  can't know a cycle overran; sleep = clamp(30..600 s,
  `CYCLE_TARGET_PERIOD` (600) − actual cycle time), capped by
  `CYCLE_TIMEOUT` = 1800 s per opencode run (raised from 1500 once timeouts
  actually kill the process tree — a timed-out cycle is expensive, an
  orphaned one is worse).
- `stage_run` — blues staggered (team1 at T+0, team2 at +300 s) AND LLM calls
  serialized via the lock; both are needed because the shared local endpoint
  has few slots and rejects oversized prompts, so the two blues must never
  call it at the same time.
- `monitor_loop` — snapshots every `MONITOR_INTERVAL` (300 s) starting at T+0
  (not T+15 — down-minute math and the report need the full window).
  `scoreboard-state.jsonl` is the structured source of truth (blue deltas,
  down-windows, `scrim-report.py`); `monitor.log` keeps the human-readable
  text. In-run red evidence: `events.jsonl` is grabbed at EVERY snapshot too —
  a teardown-time scp flake must not be able to lose it a third time.
- `blue_cycle_prompt` — cycle scope: notebook first, then AT MOST TWO
  change/fix actions (read-only investigation is budgeted but unlimited by
  the cap — the cyberfield E2E showed blue's hunts are where the learning
  is), then STOP; priority order is restore > inject due within 30 min > one
  hunt item from the checklist. The prompt states an explicit ~25-minute
  wall budget with a wrap-up-by-minute-20 mark so interrupted cycles still
  leave the notebook current. ROE embedded in every prompt: never attack the
  engine (`$ENGINE_IP`), never change the scoring-check accounts
  (triage/svc-imaging/wardops). Exact reach-your-boxes and
  scoreboard/inject-submit commands embedded per the module lessons.
- `scoreboard_delta` — "CHANGES SINCE LAST CYCLE" from
  `scoreboard-state.jsonl`: new DOWN → "RESTORE IT FIRST"; back UP → "expect
  red to re-attack it"; still DOWN annotated with since-T+.
- `inject_brief` / `due_in_min` — one line per inject with submission state;
  unsubmitted + due ≤30 min is flagged "<< TASK #1: submit before close".
  `due_in_min`: the engine's timezone is not guaranteed, so accept whichever
  of the UTC/local interpretations lands in a sane window (−15 min … +12 h).
- `NOTEBOOK_TEMPLATE` — blue working memory (SNAPSHOT / OPEN INCIDENTS /
  HUNT CHECKLIST / DONE); the SNAPSHOT line ("current state + next action")
  is first because a new cycle needs warm context instantly — the winning
  team1 notebook in the cyberfield E2E converged on exactly this shape on
  its own. Embedded in each cycle prompt (truncated to 2500 chars) plus the
  last 5 `LOG.md` lines; the notebook update is cycle step 0. An existing
  `NOTEBOOK.md` is preserved across re-runs; `LOG.md` restarts each run.
- `stage_red` — red aggression posture (2026-09 revision): round 3 bounded
  red at ~25–30 decisions (3-min windows) and 1/3/4 simultaneous takedowns on
  8 services/team; the revision tightens `decision_window_min` to 2 and ramps
  concurrent-down 2/4/6 (endgame 6 at T−15) because faster decisions + a
  higher ramp make the event a tug-of-war. `min_standing_services=2` stays
  the floor so a team always keeps two lifelines. Re-tune after the first
  rehearsal, not before (see `docs/rehearsal-gates.md`). Red01's cluster
  wiring is flag-driven, not hardcoded: `--red-ip/--red-gw/--red-storage`
  default to the Realm (10.0.0.198 / 10.0.0.1 / hdd) and
  `--red-vmid/--red-template` exist for clusters where the defaults collide
  (the cyberfield port needed vmid 999 + `base-ubuntu24.04-fix` +
  `hdrives-zfs`; hardcoding those briefly broke Realm runs). Against local
  endpoints the generated bad-auto config uses `timeout: 120` and
  `json_retries: 0` (cloud keeps 240/1): one decision that can cost 8–16 min
  turned the cyberfield red into ~2 actions/hour.
- `stage_red` (LLM gate) — `validate-llm` + the dry-run run on the OPERATOR
  and prove nothing about what red01 can reach: the dress rehearsal ran a
  whole event red-LLM-less because only the operator side was ever tested,
  and the endpoint (tailnet llama box) has no route from red01's LAN. Two
  closures: `--red-tunnel` (default `auto` = on for non-openrouter endpoints)
  runs a reverse SSH forward — red01 dials `localhost:8180`, the tunnel
  carries it to the endpoint, `ServerAliveInterval` + a 15 s supervisor
  restart it if it dies (a dead plain `ssh -R` is how the dress lost red
  mid-event) — and red01's config gets the localhost form of the URL only
  after the operator-side validate/dry-run used the real one. Then,
  post-deploy, `check_red_llm` curls `/models` FROM red01 (direct, then
  through the engine jump) and a non-200 aborts before T0: starting the event
  red-LLM-less is the one failure this harness refuses to repeat. And because
  the dress run's tunnel died MID-event unnoticed, `monitor_loop` now re-runs
  that same from-red01 probe every 300 s tick (`red_llm_watch`) and alerts
  once per failure episode — it complements, not replaces, the tunnel
  supervisor.
- `stage_red` (t0 honesty) — the event clock (t0) is captured AFTER stage_red
  returns, not before: red01 deploy + validate-llm + dry-run + the LLM gate
  can eat 5–30 min, and counting that against `--duration-min` made short
  events shorter than advertised (fixed 2026-09-23; the T0 log line prints
  how long red setup ran outside scored time).
- `reanchor_injects` (T0 honesty for inject clocks) — inject offsets resolve to
  absolute times at phase-7 deploy time, so a `--skip-deploy` restart or a long
  staging gap reaches T0 with every inject already expired (shakedown-5x4:
  blue's inject score was a guaranteed 0). Right after t0 is captured, the
  comp's offsets are re-resolved at now and pushed to the engine via
  UpdateInject (`POST /api/injects/{id}`), matched by title, every existing
  attachment re-listed under keep-files (unlisted attachments get deleted).
  Unconditional and idempotent — a fresh deploy with a fast staging re-anchors
  to the same values; unordered offsets abort before the event.
- `stage_red` (state dir) — operator-side bad-auto runs (validate-llm /
  dry-run / deploy bookkeeping) need a writable state dir: the VM's own
  config hardcodes `/var/lib/bad-auto` inside red01, so the `BAuto_STATE_DIR`
  override under the run dir affects the HOST side only.
- `stage_red` (network) — cloud LLM goes direct from red01, no tunnel needed;
  reverse tunnels exist only for tailnet-local endpoints.
- `stage_red` (red identity) — red's network mode comes from bad-auto's
  `deploy.red_mode` (flags `--red-mode/--red-subnet/--red-seg-ip` pass
  through): **routed** (default) gives red01 a dedicated segment (default
  `10.200.0.0/24`, red01 at `.10`, engine holds `.1` on its mgmt iface) and
  forwards attacks with the source address intact, so blue can hunt and
  firewall red while scoring keeps sourcing from the team gateway; **masq**
  (legacy) masquerades red as the team gateway, unblockable-by-IP. Verify a
  routed event with `verify-competition.py --red-identity` (holds a red01→box
  connection and checks the box sees the segment IP as the peer).
- `pull_red_evidence` — red01's `events.jsonl` is the only complete record of
  what red did; fetch it (plus `world.json` and the bad-auto journal via
  `sudo -n journalctl -u bad-auto`) BEFORE `badauto destroy` erases it. The
  direct operator→red01 path flaked in the 09-17b run (scp hung past 60 s),
  so fall back to the scoring engine as a jump host — the engine and red01
  share a subnet by construction. `pull_red_snapshot` is the same best-effort
  pull in-run (never raises, 75 s timeout, direct then jump).
- `_red_ssh_ctx` — red01 IP defaults to 10.0.0.198, read from bad-auto's
  `config.yaml` (`deploy.red_ip`) when present; jump-host ProxyCommand built
  from `credentials.txt`.
- `stage_capture` — runs after the event window closes, when team sessions
  have been churned all event: `qget` re-logins and retries on
  `{"error":"Forbidden"}`, and falls back to the team account if admin itself
  is locked out. Also pauses scoring best-effort (`POST /api/engine/pause`)
  to freeze the final state, freezes `scoreboard-state.jsonl` into evidence
  (it feeds the report's down-minutes/restore math), and archives blue
  LOG/NOTEBOOK/feed.log, `sub-*` deliverables, and submissions/cycles dirs.
  Since 2026-09-29 it also dumps `evidence/final-scoreboard.json` (teams,
  injects, per-team service states — all `--teams`) BEFORE the pause, because
  stage_teardown destroys the scoring DB and the report renders its "Final
  scores" section from that dump.
- `stage_teardown` — red evidence pull first, then `badauto destroy` (red01 +
  engine firewall rules, best-effort — strips both routed- and masq-mode rule
  families), then `destroy-competition.py` unless `--keep-range`.
- `MYBOX` helper — Linux boxes over key auth, Windows over password
  (sshpass), all through the engine ProxyCommand gateway; blues are told to
  prefer it over hand-building ssh.

### Mid-run box resets

A box that dies mid-run no longer means choosing between eating the downtime and killing the
run. The harness keeps running while the operator resets the box from a second shell:

```bash
python3 redeploy-competition.py --competition <id> --teams N --boxes web01 --mode reset --yes
```

`--mode reset` is the cheapest-that-works ladder (tz-ready rollback → tz-base rollback +
replant → golden rebuild, escalating only the boxes a health probe leaves unhealthy — see
[usage-agents.md](usage-agents.md)). Safe to run against a live scrim:

- the engine lock the reset takes is process-lifetime and serializes only other
  engine-touching operations (another deploy, engine-recovery) — the harness's own scoring
  and agent traffic is unaffected;
- the box's scored services go DOWN while the reset runs and the scoreboard picks the box
  back up within a round or two; blue's watchdog may flag the box — expected, not an incident;
- the replant/rebuild rungs re-take `tz-ready` when they succeed, so a later cheap rollback
  lands on the same as-delivered state;
- `--reset-event` composes with `reset` (fresh scoring DB + phase-7 reseed after the ladder)
  for full scrim reruns.

## scrim-report.py

Reads a run dir (`evidence/red/events*.jsonl` + `world.json`,
`blue-team*/{feed.log, LOG.md, NOTEBOOK.md, REPORT.md, submissions/, sub*.md}`,
`scoreboard-state.jsonl` when the run produced one) and writes
`INTERACTION.md` into the run dir. The harness now runs it at teardown (best-effort), and
teardown runs it too when the harness never got there; either way `artifacts_ops.ingest_verdict`
folds its verdict and gate table into `test.json`, and `REPORT.md` quotes that verdict instead of
recomputing one (`FINDINGS.md` is still the hand-written narrative, now superseded in the standard
folder by `REPORT.md`).

- Interaction-score philosophy (module docstring): the score is
  **restorations + red restore-reactions + evictions + injects +
  eradication**. A run that scores 0 is a FAILED RUN no matter how good red's
  kill log looks — red-vs-empty-room is not a red success; the verdict line
  calls it "parallel monologues. Red and blue never touched the same game."
- `STALL_SEC = 600` — a stall is >=10 min without a successful new action.
- `REAKILL_GAP_SEC = 900` — a re-attack on the same target after >=15 min
  implies blue restored it in between.
- `RESTORE_TTR_GATE_MIN = 15` — time-to-restore gate value; the gate itself
  lives in `docs/rehearsal-gates.md`.
- `GATES` — the machine source of the rehearsal gates; `docs/rehearsal-gates.md`
  is the human-readable contract. Tune after the first rehearsal, not before
  (a gate nobody has failed yet is a guess).
- `EXPECTED_17C` — self-test pin (`--self-test`) verified against the
  agent-scrim-2026-09-17c run's FINDINGS.md evidence: takedowns 4,
  distinct_tactics 3, restore_reactions 1, restorations 1, injects 0,
  interaction_score 2, stalls 2. If metrics change, re-verify against
  FINDINGS.md before re-pinning.
- `parse_ts` — event timestamps are red01's clock, treated as UTC
  (`calendar.timegm`), optional fraction.
- `load_red_events` — merges the final `events.jsonl` with the in-run
  snapshots, filtered to this event's window (stale round-N leftovers sit
  outside [T0, T0+8h], with a 120 s grace before T0) and deduplicated
  (snapshots overlap the final file by construction; dedup key =
  ts/kind/tactic/target/detail). T0 comes from `load_event_start`
  (`world.json` `meta.event_start`, in evidence or `bad-auto-state/`).
- `load_scoreboard` — returns `[]` when the run dir has no
  `scoreboard-state.jsonl`. The report then runs in no-scoreboard mode:
  down-minutes, TTR, and max-simultaneous-down are n/a, the restoration count
  is inferred from red's re-kills and marked as such, and the honest red-side
  numbers carry the verdict.
- `HOST_BY_OCTET` — last octet → host for the scrim subnets
  192.168.10X.0/24 (identifier 10X = team X−100, generic since 2026-09-29 —
  `host_label` maps any 101–199 third octet; the old table only knew
  101=team1, 102=team2): 2=dc01, 3=win01, 4=web01, 5=app01,
  6=db01.
- `takedown_fields` — prefers structured `data` (ip/unit/service/mode); falls
  back to regexing the detail string ("X is DOWN on ", first IPv4); mode
  defaults to "firewall" for `impact_firewall`, else "stop".
- `foothold_list` — world.json's "footholds" has been dict-keyed-by-ip or a
  list across versions; accept both.
- `red_metrics` restore-reactions — a takedown on a target red already killed
  >=15 min (`REAKILL_GAP_SEC`) ago implies blue restored it in between; works
  without a scoreboard series. Runs also carry explicit `blue_restore` events (counted
  separately as `blue_restore_events`).
- `red_metrics` stalls — gaps between successful non-health_check actions;
  the OPENING (T0 → first success) counts too, so a red that sleeps through
  its first 20 minutes can't hide behind a dense later log. (The
  health_check exclusion and the opening count are rev-2 corrections — 17c's
  dead first 29 minutes was its biggest stall and the naive
  gap-between-successes definition missed it; see
  `docs/reports/harness-upgrades-plan.md`.) With **no clock** (a run dir with
  red events but no `world.json`/`event_start`) the stall maths is skipped and
  reported as none — it used to abort the whole report, hiding the `timeouts`
  count the rehearsal gate reads.
- `red_metrics` windows_footholds — from world.json footholds' `windows`
  flag; when none carry it, inferred from initial-access events against the Windows
  boxes' last octets in the competition's `boxes.json`.
- evictions — counted as 0 in the score: not yet observable (metric gap);
  rendered as "evictions n/a".
- `down_windows` — per team: restorations (real DOWN→UP transitions between
  snapshots), TTRs, down-minutes, max simultaneous down; a service still down
  at the last snapshot is closed at event end and counts toward down-minutes
  (which is why `monitor_loop` must snapshot from T+0).
- `blue_metrics` — cycles rc=0 parsed from `feed.log` `===== cycle ... rc=N
  =====` headers ("MANUAL" prefix counted separately); notebook_entries =
  non-heading `LOG.md` lines + checked `NOTEBOOK.md` boxes; eradication = log
  lines matching beacon/backdoor indicators (tznet, svc-netupdate, TzNet,
  red_key, authorized_keys, backdoor, rogue, uid 0, unauthorized) AND a
  removal verb (removed/deleted/disabled/uninstalled/locked/changed-back/
  reset); injects = `sub-*.md`/`sub-*.txt` plus everything in `submissions/`.
- `compute_components` — score components alone, for the pinned self-test;
  a pin mismatch prints "re-check against FINDINGS.md evidence".

## beacon_ops.py

Plants raw-socket beacons on team Linux boxes as blue-team hunt artifacts,
during deploy phase 6 of a competition that enables Compfile
`team_beacons 1`.

- Module docstring — each beacon is a compiled copy of
  `artifacts/rawsockets-beacon/beacon.c` run by an innocuous systemd unit
  (`wda-digest.service`, "Wardline Data Digest Agent", `Restart=always`,
  `RestartSec=20`), sending forged SYN packets with a BEA1 payload at the
  team gateway (`192.168.<tid>.1:4444`) on a per-box interval. Beacons are
  NOT scored, NOT destructive — the exercise is for blue to find the
  process/unit/binary and its periodic egress and shut it down properly;
  stop alone won't stick (service restarts / the beacon's own loop). Planted
  BEFORE the tz-ready snapshot so team2+ clones and restore points inherit
  them.
- `BEACON_INTERVALS` — cadence varies per box (web01 45 s, app01 60 s,
  db01 90 s) so the periodicity signature isn't uniform.
- `_build_beacon` — static build for the operator host if possible (no
  toolchain needed on the boxes); a dynamic local build won't run on the
  Ubuntu boxes, so failure here means on-box compilation (the install script
  then apt-installs gcc if the box lacks cc/gcc and builds there).
- `_ssh_box` — with `sudo_password`, cmd runs as root via `sudo -S bash -s`
  (password + script on stdin) — needed because the planted writable-sudoers
  misconfig breaks passwordless sudo on some boxes.
- stdin-consumption gotcha (`plant_team_beacons` install step) — ship the
  script as a FILE (`/tmp/.wda-install.sh`) and run `sudo -S bash <file>`;
  with the password on stdin, sudo consumes it only when prompted — when
  NOPASSWD applies the stdin line is unused, so a script traveling on stdin
  would have the password line execute as a command (on boxes where the
  planted writable-sudoers misconfig broke passwordless sudo).
- `plant_team_beacons` — warn-and-continue per box: beacons are scenario
  flavor and must never abort a deploy. Success signal is `systemctl
  is-active` printing "active". Artifacts left on the box:
  `/usr/local/lib/.sysmon/beacon`, the unit file, and `/etc/.sysmon.conf`
  (`C2=<gw>:4444`, `agent=wardline-<box>`).
- `_scp_to_box` / `_gateway_proxy` — all box access goes through the scoring
  engine as SSH ProxyCommand, same gateway pattern as the rest of the
  pipeline.
