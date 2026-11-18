# Night 4 — agent-issue investigation, then the real cde-2026 re-run

Started ~21:29Z 2026-11-17 (daytime handoff from the run-84c0e45d night).

## What I found (agent issues from run-84c0e45d, condensed)

1. **Every cycle timed out because the LLM stream died, not because the cap was
   too short.** Blue-team1's `last-timeout.partial.log` ends with opencode's own
   retry trail: `ECONNRESET … attempt 2 … attempt 3 … HTTP 524` — the club
   gateway's stream dropped mid-cycle repeatedly, and the retry storm burned the
   whole 1200 s with no work completing. The models themselves are healthy now:
   all three (`k2`, `qwen3.8-flash-next`, `qwen3.8-27b`) probe ~3.5–4.5 s for a
   tool call and stream 2000 tokens in 31–45 s with zero drops.
2. **Both blues serialized behind ONE lock because locks keyed on the URL only.**
   Two teams sharing litellm.dawgsec.com shared a lock regardless of model. Fix:
   `scrim/event_run.py` keys the lock by (base_url, model) — gateway models
   multiplex, a llama.cpp URL is still one lane. Commit `969bf8b`, tests in
   `tests/scrim/test_agent_lane_locks.py`. Negative-verified: old code passes
   only because the fixture sets a cap.
3. **The red campaign's 37-vs-3 takedown imbalance was a dead guard.**
   `tlaloc/policy.py`'s `need_breadth` was gated on `bool(self.max_down_now())`
   and the DEFAULTS cap is 0 (no cap) — so the breadth switch never fired in an
   uncapped comp, the +40 re-hit bias ran unopposed, and blue team2 (which
   restored) sucked all the pressure while team1 (whose restores never scored a
   DOWN->UP edge the guard could see) went untouched after T+13. tlaloc's own
   endgame summary printed `impacts_by_team: {team2: 37, team1: 3}` — the agent
   KNEW and no gate read it. Fix in `16140d9`, uncapped regression test added
   (fails on old code — verified by stash/re-run).
4. **Blue wasted cycles on a PowerShell quoting trap.** team1's partial log shows
   a whole lost cycle on `Where-Object {\$_.Name ...}` — through `./mybox` the
   backslash reaches PowerShell literally. The prompt now shows the single-quoted
   wildcard form (`bf89681`).

## Testing (user: distribute evenly across flash-next / qwen27b / k2)

`.night4/cycle_bench.py` runs realistic multi-tool cycles through the
PRODUCTION path (`blue_agent._opencode_run`, same staging hygiene, cap-killing,
server reaping) against local fixtures. Results (12 cycles, 4 per model):

- Phase A serial: k2 87.5 s rc=0, flash-next 290.9 s rc=0, qwen3.8-27b 191.9 s rc=0.
- Phase B1 (3 parallel, one per model): k2 53.4 s, 27b 126.8 s, flash-next 401.7 s — rc=0 all.
- Phase B2 (6 parallel, 2 per model): 92.5–226.3 s, rc=0 all. **Zero timeouts, zero
  ECONNRESETs, zero 524s across 12 cycles.** Every workdir ended with REPORT.md + DONE.
- Full suites green: engine 543 passed (`tests/scrim verify deploy artifacts c3rberus`,
  +3 new lane-lock tests), tlaloc hermetic suite green, dashboard runner tests green.

## The real run

**Twin check, 21:53Z:** another session started a `scrim-one-light` deploy
(vmid 3300, ids 152,153, mgmt 10.0.0.254, --hold-start) seconds after mine.
Zero overlap with cde-2026's set (1278 / 127,128 / 10.0.0.251 / red01 1661 @
.247) — distinct engine ⇒ distinct engine lock. Documented, not interfered
with. Residual shared surface: the litellm gateway — if that session runs agent
scrim cycles while my event is live, my lanes gain a fourth contender; the
three-model split already minimizes per-deployment concurrency to 1.

`run-2cd2e47c` submitted 21:45Z: scrim-with-deploy, cde-2026, 2 teams, 90 min,
cycle_timeout 1200, blue-watchdog, keep-templates, scoring_vmid 1278, ids 127,128,
engine-mgmt 10.0.0.251. Models spread across the three the user permits:
**blue1 = qwen3.8-flash-next, blue2 = qwen3.8-27b (own lane via --blue2-*),
red operator+campaign = k2.** Engine head bf89681 = everything above.

6th fix, tooling: last night's ledger export recorded `scoreboard`/`health` as
404 because `.night3/platform_tools.py` GUESSED the paths (`/scoreboard/status`,
`/health`). The platform's actual FastAPI routes (read off the source that
teardown collected into run5's `red-c3rberus/c3rberus/`) are `/scoreboard`,
`/scoreboard/history` — per-team scoreboard history, the exact per-team balance
data that was missing from the imbalance diagnosis — and `/nxc-health`. The
night-4 copy `.night4/platform_tools.py` exports all five sections.

Blue2 flags ride the submission in `extra_args` because the RUNNING service
process loaded runner.py before `0e80b21` landed; the whitelist fix is for every
future submission. (Dashboard main carries 3 pre-existing red tests — the
`mgr.submit()` websocket-import guard fails under a bare `/usr/bin/python3`
pytest while the service venv has websocket; environment artifact, not this
task's, not silently "fixed" from under its author.)

## 7th fix (during tonight's deploy): red balance gate

Tonight's run is on `bf89681`; while its deploy provisioned goldens I landed the
follow-up named in the night-3 report. The aggregate `takedowns` gate is
satisfiable by hammering ONE team — run-84c0e45d was green on every red gate at
37-vs-3. New gate `min_team_takedowns`: red_metrics now attributes each takedown
to a team by its subnet identifier (same octet host_label uses), and the gate
demands the least-pressured fielded team received >= half a fair split
(render-derived; static floor 1 for small runs; n/a when no team map resolves,
never a false pass). 4 new cases in test_rehearsal_gates.py incl. the exact
37-to-3 replay. Docs: same pass corrected the two stale "one shared LLM lock"
lines for the per-(endpoint, model) keying from 969bf8b. Commit `2ea91ba`.

This gate will NOT show in tonight's INTERACTION.md — the run's teardown renders
from its pinned bf89681 worktree, ahead of which this landed. Tonight's report
states the distribution by hand from the journal; the gate makes it automatic
next time.

## Deploy diagnostic record (22:23-22:41Z "stall" that wasn't)

Phase 4 went silent for ~17 min with no ssh child visible. `py-spy dump` on the
deploy pid showed two `boot_win` workers parked at `windows_ops.py:193` — the
BOOT-TIME-POLLING wait inside the feature-install reboot path (pending-role
installs keep a box InstallPending; the loop polls boot time up to 300 s,
escalates to a forced `reboot_vm`, then `wait_for_guest_agent` 600 s). Bounded by
design; it resolved at `bootstrap_windows 192.168.128.2 = 1316 s` (a Windows
feature re-bootstrap, the long pole). Lesson for the tick playbook: absent ssh
children ≠ idle — dump the thread stacks before calling a quiet deploy stuck, and
note `staging.py`'s `capture_output` hides the deploy's live log until the phase
returns (timings / `/proc` / py-spy are the only live view). No code change
warranted: the bound exists, the fallback fires, the outcome is correct — the only
cost is report-visible latency. `~/.local/bin/py-spy` is the diagnostic; worth
adding to the runbook.

## 8th fix — the real run's first attempt DIED, and why (run-2cd2e47c)

The 21:45Z scrim deploy failed at phase 7 after ~1h31m, and the message lied:
"red01's disk could not grow to 60G — the C2 needs it (ssh: connect to host
10.0.0.247 port 22: Connection refused)". Root cause chain, evidence in red01's
own guest:

1. cloud-init's boot-time `package_upgrade` ran 314 s and FAILED —
   `TimeoutError: Could not get APT lock`.
2. openssh-server's postinst stopped `ssh.socket` at 23:11:02Z; because the
   upgrade then failed, it was never restarted: `ssh.socket` inactive, `ssh`
   disabled, nothing listening on :22, while the qemu guest agent answered fine.
3. `red01_ops.deploy` never proved ssh — it is the ONE provisioned box that
   skips the `wait_for_ssh` every other path uses (engine, template builds,
   redeploy, gcp). `resize_red_disk` dials once, so the first consumer of the
   channel reported the failure as a disk problem.

Fix 1 (`0e32153`): bounded ssh proof in `red01_ops.deploy` on the same argv the
consumers use, with the guest agent kicking `ssh.socket` when nothing listens, and
an error that names ssh instead of the disk.
Fix 2 (`7eb3231`): the first resume (`run-c62529ee`) then failed honestly —
"never accepted SSH at 10.0.0.247" — because the upgrade's reboot dropped the mgmt
NIC's IPv4 entirely (guest showed only `lo` + the red NIC). The netplan pin that
exists FOR that reboot ran AFTER the ssh wait, so the wait could only time out.
Reachability has to come from the agent channel first; ssh is proven after it.
Third resume (`run-4f84acff`, head `7eb3231`) cleared it: `c2: red01 disk 60G ->
58G`, sliver installing.

Tests: 3 new in `tests/red/test_red01_ops.py` (ssh proven before hand-off; a dead
socket started through the agent; the failure naming ssh, not the disk), all three
negative-controlled against the old code (3 failed before the fix). red+deploy
suites 256 passed.

## Correction + the real event

Earlier note said the balance gate wouldn't reach tonight's report because
teardown renders from the pinned `bf89681`. That was right for the failed first
deploy but NOT for the event that actually runs: the range was resumed twice and
the scrim row is `run-46f88c2d` at head **7eb3231**, which contains 2ea91ba — so
INTERACTION.md tonight DOES carry `min_team_takedowns`, and red_metrics will
report the per-team split off tonight's own journal.

Range: deploy `run-4f84acff` (resume of the dead `run-2cd2e47c`) succeeded rc=0 at
03:06:39Z — phase 8/8, engine live, 203 active IOC rows across all 8 boxes at
plant time (live census), templates verified PASS (engine 0d7b38877b48, goldens
d9b789673938/81ad4cea1329/638999af7732/f44203581c09).
Event: `run-46f88c2d` — --skip-deploy on the same workspace, 90 min, cycle_timeout
1200, blue_watchdog, keep-templates, blue1 qwen3.8-flash-next, blue2 qwen3.8-27b,
red k2.

## 9th fix — the scrim's own deploy was releasing red before T0 (run-46f88c2d)

The 90-minute event never started: `verify_red_reaches_teams` refused it with
"the pre-T0 scoreboard is not green — 4 scored pin(s) DOWN: team1:ad01-ldap,
team1:db01-sql, team1:ftp01-ftp, team2:ftp01-ftp". The gate was right; the range
was damaged before the bell. Evidence chain:

- red01's auth.log: `systemctl stop mysql && systemctl mask mysql` as blueteam at
  **03:11:03**, preceded (0.2 s) by a unit-discovery probe — tlaloc's own
  `stop_mask` recipe.
- tlaloc's events.jsonl lists five takedowns, all `stop_mask`, all `ok`:
  127.4 apache 03:10:54, 127.2 ADDS 03:11:03, 127.3 IIS FTP 03:11:06,
  127.5 mysql 03:11:08, 128.3 IIS FTP 03:16:17.
- `tlaloc.service` started at **03:06:27** — `systemctl start tlaloc.service` as
  sysadmin, i.e. the deploy's `begin_red_after_delivery` ran and did NOT hold.

Root cause: the deploy ends by releasing red unless `--hold-start` is on the
command line, and the DASHBOARD's `deploy` action passes it while a **scrim's own
deploy child (scrim/staging.py) never did**. So a scrim-with-deploy has red
attacking from phase 8 until stage_red stops it again at 03:18:02 — twelve
minutes of unscored play that broke the estate for the event that followed. (A
separate deploy row + `--skip-deploy` scrim — last night's shape — never showed
this, which is why it survived to tonight.)

Fixes (`e8eecb7`): staging.py's deploy child always passes `--hold-start` (the
scrim owns T0 and releases red itself), and `begin_red_after_delivery` now also
honours `red_loop_held`/`hold_start` recorded in the state document, so a RESUME
cannot silently release a held range either. Test: `DeployStagingHoldsRed`
(negative-controlled — fails without the flag).

Recovery: `redeploy --mode reset --boxes ad01,ftp01` (run-89ec9321) rolls those
boxes back to their as-delivered `tz-ready` snapshot — the engine's own ladder,
one rung above a rebuild — rather than hand-restarting masked services. db01's
mysql I had already unmasked+started by hand before that decision; the reset
covers the Windows pins.

## Event `run-be6e9f6f` — mid-event read (T0 04:13:54Z, bell 05:44Z)

Cycle outcomes so far, straight from the scrim's own log:

    [23:24:30] blue-team2 cycle T+5  rc=0      (qwen3.8-27b)
    [23:26:42] blue-team1 cycle T+0  rc=0      (qwen3.8-flash-next)
    [23:29:10] blue-team2 cycle T+13 rc=0      (qwen3.8-27b)
    [23:37:54] red-c3rberus cycle T+4 TIMED OUT at the cycle cap (k2)
    [23:47:13] blue-team1 cycle T+13 TIMED OUT at the cycle cap (flash-next)

Versus run-84c0e45d: 11 cycles, 0 rc=0, all dead at the cap on stream drops.

The two timeouts are NOT the old failure mode — both partials carry ZERO
ECONNRESET/524/retry signatures (grep -c = 0 on each) and both agents were
doing real work at the boundary:

- blue1/flash-next was mid-investigation with a genuine find: it deleted the
  attacker's "blk ldap" firewall rule on ad01, re-enabled FTPSVC (attacker had
  set StartType=Disabled), submitted inject #1 at T+6, and at T+16m discovered
  `red_scoring` in ftp01's local Administrators with a LastLogon timestamp
  inside the same cycle — a real red foothold, then the cap cut it.
- red/k2 was driving the c3rberus platform over its API (POST
  /c2/iocs/deploy-and-register -> task_id 66) after reading the platform source,
  i.e. a long multi-step operator turn.

So the failure mode this mission set out to kill (endpoint stream death burning
the budget) did not occur; what remains is scope-vs-cap: a lane that chooses an
open-ended hunt or a multi-step platform task can still exceed 1200 s while
working correctly. That is a calibration question for the next pass (cap per
lane, or a cycle budget the prompt respects), not a defect to hot-patch
mid-event.

## Outcome (run-be6e9f6f, artifact run-808cfc2c-run2 — SEALED and clean)

Verdict: **NOT READY — interaction score 65; 11 gates PASS / 1 FAIL**
(previous run: 59, 8/3). The single failing gate is `blue.timeouts 4 > 2`.

The failure mode this mission existed to kill is gone: last run 11 cycles / 0 rc=0
all dead on ECONNRESET/524; tonight 7 rc=0 blue cycles + 3 rc=0 red-operator
cycles, 24 restorations, 37 restore events, 2 injects, 5 eradications, 18 notebook
entries — and grep -c on every timeout partial = 0 for ECONNRESET/524/retry. The
four cap-timeouts were lanes doing real work when cut (flash-next's account hunt
that found `red_scoring` in ftp01's Administrators; k2 driving the platform API).

Red balance: 26/8 per team (was 37/3) and `min_team_takedowns 8 >= 8 PASS` — the
new gate reads the split the old aggregate gate could not.

Artifacts: archived to tezcatlipoca-competitions/cde-2026/.automated-tests/
run-808cfc2c-run2 (111M), REPORT.md authored + `verify --seal` = "PASS — sealed and
clean" (46 files re-hashed, 0 problems). Session report:
tezcatlipoca-dashboard/scrim-four-agent-cycles-red-team-report.md (commit 25fa6ef).
