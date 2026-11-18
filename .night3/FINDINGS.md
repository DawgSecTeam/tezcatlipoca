# night-3 findings (running)
- Mission's deploy payload (deploy + comp_dir) is REFUSED by design — dashboard/runner.py:342.
  Deploy submits without comp_dir; workspace seeded from comps repo. run-4f8dce5f/comp's
  packet.yaml names base-fedora44-fix/base-ubuntu24.04-fix — absent on hdd-150 (1016/955 are
  the real ones); only those 2 lines differ from the repo packet, credentials byte-identical.
- Engine cb8f97d4: goldens rebuilt on hash drift (3 new commits) = +~30 min vs a reuse night.
- Probe-tool hygiene: red01 IP is recycled per deploy → host key changes. Engine ssh_argv
  already uses UserKnownHostsFile=/dev/null (red01_ops.py:140); manual probes must match it
  or ssh's MITM guard can kill a T+75 export. Fixed in .night3/platform_tools.py, verified.
- Phase-7 silence at "red rules : installing..." then the c2-prep matrix = SILENT by design
  (one _ssh_run, 6h cap, tlaloc_ops.py:700-717). Prove liveness on red01: pgrep -c go build.
- 00:56:56Z COLLATERAL: a twin session restarted the dashboard service (it is running its own
  sliver-practice-4box resume_deploy, run-ebbc1f3d). The new instance's startup reconcile
  closed MY live deploy row as "failed: orphaned: no worker after service restart" — while the
  driver kept working. Proof of life, in order of strength: comp/.deploy.lock flock held
  (rc 1 under `flock -n`); c2-prep alive on red01 under an established ssh from 10.0.0.143;
  run.log written via the child's own fd (runner.py:831 open(log_path,"w") → Popen stdout=log_fh),
  so the service's death cannot stall or stop it.
  => DEFECT (dashboard): reconcile_orphans assumes "no Popen in this lifetime" == "no worker".
  A setsid/nohup-restarted service kills nothing; the correct test is pid liveness + cmdline
  match against the run's worktree, and re-adopt (or at minimum label the row "orphaned-alive",
  not "failed"). NOT FIXED TONIGHT because shipping it needs a restart, and a restart would
  orphan the twin's moving deploy — the blast radius outranks the fix until the estate is idle.
  => Operational rule adopted: for an orphaned-but-moving run, judge from run.log + flock, never
  from the API row; and never submit resume_deploy while the lock is held.
- 01:46Z "build-all-iocs failed" is CHRONIC, not a regression: every run in the store's history
  (8 sampled, incl. last night's GREEN 128 chain) prints it. Cause, from the platform log:
  "build_all complete (task=2): built=16 reused=0 n/a=54 skipped=6 failed=2 of 78" — the 2 are
  firewall_self_inject / firewall_self_inject_devd, "freebsd shared-lib generate gRPC error",
  and this lineup has NO in-path firewall (phase 5 skipped). So a milestone word the operator is
  told to watch is always "failed". Fix candidates (report, not tonight): build_all should report
  partial vs failed; the engine should print the task's built/failed counts instead of the bare
  status word; platform should skip IOCs whose target OS isn't in the inventory.
- Platform defect seen in deploy-all: netexec's ssh module crashes its own DB read —
  "(sqlite3.ProgrammingError) Error binding parameter 1: type 'list' is not supported" on
  "SELECT ... FROM loggedin_relations WHERE credid = ? AND hostid = ?" with parameters
  ([2 cred rows], hostid) — i.e. a LIST of credentials is bound where one id belongs. Surfaces
  as "nxc failed for ssh <ip> (exit 0)" + "Deploy script status=-1: No valid JSON output from
  check script" for linux IOCs. Duplicate red_scoring cred rows (id 1 and 2, same user/pass) is
  the trigger — provisioning writes the cred twice on a fresh DB.
- MIRA-128\red_scoring STATUS_LOGON_FAILURE seen on team2 Windows during deploy-all (local
  red_scoring fallback is what lands those deploys). Watch the estate gate for N/8.
- 02:18Z CENSUS BASELINE (pre-event, for the T+75 growth test): tasks total 60 =
  {failed 38, done 16, running 5, skipped 1}; deployed rows 250, ALL active; per box
  24 (ad01) / 31 (ftp01) / 35 (web01) / 35 (db01), symmetric across teams 127/128.
  The 96 deploy_failed DB rows (27.7%, inside the tolerated band) all carry one message:
  "Deploy script status=-1: No valid JSON output from check script". /c2/iocs/deployed
  returns only the active ones. Seed doc on disk holds {admin_password, scoring_password,
  red_ip} until stage() finishes (c3rberus_ops.py:580 vs :638) — census works off
  admin_password, so it is usable mid-stage.
- SELF-INFLICTED, RECORDED HONESTLY: at 02:18Z I ran `pkill -f "tail -F .../run.log"` to
  retire a watcher that had ALREADY exited on its own match; the pattern was in my own
  invoking command line, so my shell got SIGTERM (rc 143) — the exact trap AGENTS.md names
  ("kill by pid or process group, never pkill -f a pattern that also appears in the invoking
  command line"). No harm to the run (the driver is a separate process group; the deploy
  lock stayed held), but the rule earned its keep again tonight.
- 02:32Z DEPLOY FAILED at the estate gate — ROOT CAUSE WAS OURS, not the platform's:
  plant() graded a box as catalog-carrying with n >= max(12, int(0.8*median)). Tonight's
  histogram [24,24,31,31,35,35,35,35] has median 35 → threshold 28 → the two COMPLETE
  domain controllers (24 rows, 24/24 attempted = 100% coverage) read as "not planted" and
  the run raised "only 6/8 catalog-carrying boxes". The comment above that line says the
  relative test exists to avoid mis-grading a small-but-complete box; anchored to the
  median it does the opposite. The old uniform-row test (60/box) could never see it.
  FIX 67736e2 (pushed to main): the median may only LOOSEN (n >= 12 or n >= 0.5*median),
  never tighten past the floor. tests/test_c3rberus.py gained both directions, and the
  new test reproduces tonight's exact line under the old expression ("250 active rows,
  6/8 boxes carry a catalog") — 15 passed.
- 02:41Z RESUBMITTED via the dashboard: run-3ae49703 resume_deploy → create-competition.py
  --comp-dir <run-425e6b8d/comp> --from-phase 7 --hold-start --scoring-vmid 1278
  --team-identifiers 127,128 --engine-mgmt-ip 10.0.0.251. Deliberately NO from_phase and NO
  force_from_phase: the state doc records last_phase 6 (phases 1-6 completed), so the cursor
  is truth and the force hatch is for a known-stale checkpoint only — using it unneeded is
  the --force-style workaround AGENTS.md forbids. Confirmed in the log: "Resuming from phase
  7 (2 team(s), last completed phase 6)", phases 1-6 skipped.
- LESSON: verify a flag exists in the code that PARSES it, not the top-level script —
  --force-from-phase lives in deploy_lib/cli.py:47, and create-competition.py is a shim.
- 03:17Z THE FIX WORKS IN PRODUCTION: "deploy-all-iocs failed; 449 active rows, 8/8 boxes
  carry a catalog" → "day-0 catalog planted — 449 IOC deploys across 8 boxes (scoring starts
  at T0)". Same estate the first pass refused. Re-plant numbers: per box 44 ad01 / 60-61 ftp01,
  web01 / 59 db01; 128 rows deploy_failed (22%). Note deploy-all's tail is slow because the
  last stragglers each take ~12 min (tasks 49-53 created 03:04:42, completed 03:16:42) — the
  retry chain, not a stall: zero connections to boxes + zero running tasks was the tell.
- 03:36Z EVENT #1 (run-1e99baac) FAILED BEFORE T0 on the pre-T0 fire test, and the estate was
  NOT the problem: fire test log shows http=000 with httpd stopped and http=200 after restore,
  while the scoreboard read raised "ValueError: unexpected payload: None" for 14 minutes.
  Root cause, proven at the source: Quotient's `box_schemas`/`round_schemas` tables are empty
  (0 rows) until the range's FIRST `start`; /api/services/<tid> therefore answers literal null
  (HTTP 200) pre-T0, while the engine is happily running PREFLIGHT rounds over the same 9 pins
  ("Preflight round 28 complete", all result=true; GET /api/engine/preflight → checks[18]).
  Every fire test that ever passed ran on a workspace that had been started before — this was
  the pipeline's first-ever fresh-range event, which is precisely what a practice run is for.
  The verify gate "pins_registered: FAIL missing: <all 9>" was the SAME defect wearing a
  harmless costume: tests/verify/test_interim_verify_label.py taught me to dismiss it as
  interim, and for 15 minutes I did. A labeled-interim reading must never outrank a live read
  of the effective value.
- FIX 027a420 (pushed): parsed_status and check_services fall back to /api/engine/preflight
  when the scoreboard payload is null (a started range never takes that path — proven by
  test_a_started_range_never_consults_preflight), and a genuinely empty preflight still raises
  / still FAILs the gate. 297 tests pass in tests/scrim + tests/verify.
- 04:05Z EVENT #2 (run-8b3462ff) got PAST the fire test — "down_detected=True restored=True
  healed=True", "pins_registered : PASS 9 pinned checks" — so the preflight fallback is right,
  then the pre-T0 verify refused on `injects : FAIL`. Root cause, from the deploy's own words:
  "the engine stays paused and injects are deferred"; create_injects runs at `start`
  (quotient/lifecycle_ops.py:123), and /api/injects answers [] with competition_started:false.
  The engine's refusal-advice ("--allow-gate-fail telemetry,injects") would have made an
  operator name a structural fact in a CLI flag; the gate now reads the flag from the engine and
  SKIPs non-gating, still FAILing a live game with missing injects (e387eb2).
  SIDE FINDING, still open: finish.py printed "(1 inject(s) loaded)" from len(ctx.injects) — a
  local count presented as an engine fact. Same class as the pin gate: the pipeline reports
  INTENT where it should report EFFECT. Fixed in the same commit's wording, but tonight's third
  such case is a pattern worth its own pass.
  OPEN QUESTION being watched during event #3: staging logs "tlaloc seeded 1/1 box(es)" while
  the deploy said "seed manifest proves 8 box(es) planted". If red's campaign only reaches one
  box, this is a red-vs-empty-room run and the mission's bar is not met.
- 04:30Z EVENT #4 (run-7998e766) REACHED T0 and was cancelled at 04:55Z after 25 minutes, for a
  fifth defect found by reading effective values instead of the log: /api/engine answered
  competition_started:false and round_schemas/box_schemas were empty while blue was already on
  cycle T+0 with rc=0. The scrim starts RED's platform at T0 (c3rberus /admin/start_comp) and
  the deploy holds the SCORING engine's clock (--hold-start defers it to competition-ops start)
  — and a --skip-deploy scrim never walks that door either. So each half was correct in
  isolation and their combination played 90 silent minutes against preflight rounds. inject_sync
  had already papered over one symptom of this exact gap ("hold-start without competition-ops
  start?") by creating the missing injects at T0 — the tell that the missing call was known and
  never fixed. Fixed in b2d0f3b + 8c250cc (T0 now calls start_engine_clock, refuses the event if
  the clock does not start, harnesses stub it and one test asserts it runs).
  NOTE: b2d0f3b was pushed with three harness tests failing — I added a T0 side effect without
  updating the three places that drive main() and stub every other one. Caught by the suite two
  commands later; fixed in the follow-up commit. Lesson: run the tests that drive main() BEFORE
  pushing a change to main's flow.
- CANCEL decision: "never cancel a moving run" guards against killing a deploy that is making
  progress. This run could not produce a scored event at any point in its remaining 65 minutes;
  letting it reach its bell would have destroyed the estate for a phantom GREEN. Cancelled, the
  lock released, and the estate verified intact (all 18 preflight checks UP), then resubmitted
  against the same range with the fix.
- 05:01:18Z EVENT #5 (row run-fa766346, key run-84c0e45d-run5) REACHED T0 WITH A REAL SCOREBOARD:
  /api/engine competition_started=True, 9 services x 2 teams with recorded rounds, `injects :
  PASS`, c3rberus adopted live (465 rows, 8/8 boxes), "tlaloc loop released — red is attacking".
  Interaction is measurable, not inferred: watchdog RESTORED events at T+5 (128.4 web01 httpd,
  128.5 db01 mysql) and T+13 (127.5 db01 mysql, 128.4 web01 httpd), and team2's scoreboard showed
  ad01-ldap + ftp01-ftp DOWN at 05:08Z. Both agent lanes are live processes: blue-team1 opencode
  turn running, and the RED TEAM OPERATOR opencode turn driving c3rberus (pid 746428) — the
  mission's headline requirement.
- ANSWERED: the "tlaloc seeded 1/1 box(es)" line is NOT narrow coverage. During the event the
  platform's own tasks target 2-4 boxes each (52 tasks at box_count=4 = one team's four boxes,
  deploy_all at 8), so red reaches the whole estate; the 1/1 wording belongs to the skip-deploy
  seed-manifest label. Worth a doc/wording pass, not a code fix.
- platform_tools ledger export shape (for the report): sections = deployed (477 rows), tasks
  (62), scoreboard/health → HTTP 404 on this c3rberus build, so those two sections are _error
  stubs. The census + tasks carry the red lane's substance. Task fields: id, task_type, status,
  box_count, error_count, description, created_at — no per-target column, so coverage comes from
  box_count and the description's "to N box(es)".
- 05:59Z LIVE METRICS (T+57), for the report: engine competition_started=True with 10 rounds of
  history per team; BOTH teams DOWN on ad01-ldap + ftp01-ftp at the same sample (symmetric red
  pressure, not a one-sided room); watchdog RESTORED events = 20; lanes: blue-team1 cycle T+0
  (timed out at cap), blue-team2 cycle T+5 (timed out), red-c3rberus cycles T+4 and T+26 (thread
  surviving, cap hitting every cycle — same "cosmetic not functional" shape as run7); platform
  active rows 465 -> 485 (+20 net-new from red's operator), tasks 57 -> 65.
  Every cycle that sampled hit the 1200 s cap, so each lane turned ~3 times in 90 min: the cap is
  now the binding constraint on agent depth, not on infrastructure.
- 07:25Z NIGHT CLOSED. Event #5 (run-fa766346) ran 05:01:18Z -> 06:31:18Z, row succeeded,
  teardown destroyed 12 keeping templates; artifacts sealed and clean as
  cde-2026/.automated-tests/run-84c0e45d-run5. Machine verdict NOT READY, interaction score 59
  (run7 was 128): red gates 6/6 PASS (40 takedowns, 34 restore-reactions, 4 tactics, 8 footholds),
  blue gates 2/5 (cycles_rc0 0, injects 0, timeouts 4). No lane died — all 11 agent cycles hit the
  1200 s cap, and the campaign spent T+22..T+90 attacking only team2's web01/db01 (37 vs 3
  impacts). Chapter: scrim-three-held-start-red-team-report.md. Both schedules deleted, watchers
  retired.
- 08:26Z harness note: deleting schedule-c2e5e08f (the 15-min event tick) does NOT drain its queued occurrences — after the ~5 h downtime backlog the deleted id delivered stale ticks at 03:36/03:51/04:06Z occurrence times, each describing an already-dead event row. Verified harmless (no active rows, lock free), but a deleted tick that keeps waking a session is a real trap: the wake-up text looks like live state. Behavior is documented in the tool, so the mitigation is to treat a reminder whose prompt contradicts a live read as noise and re-confirm from the dashboard.
- 08:28Z self-inflicted false alarm: a stale tick prompted a liveness check written as pgrep -f "tail.*run-fa766346", which matched the shell running that command, so I reported a watcher still alive and nearly killed my own pipeline by pid. Correct form is pgrep -x tail (or filter ps by $2) — AGENTS.md already carries the rule about kill-by-pid and self-matching patterns; the lesson is that the same trap bites CHECKS, not just kills. No watchers were actually alive.
