# Two 90-minute agent scrims on .150 — analysis and improvement proposals (2026-10-03)

Two back-to-back automated scrims ran on node `proxmox` (10.0.0.150; .193 was down all day —
no route), each from its own worktree cut off `main`, each 90 minutes, red and blue both
LLM-driven (openai/gpt-5.6-luna via OpenRouter):

| | run 1 | run 2 |
|---|---|---|
| competition | `scrim-fresh-a` (authored fresh from `same-type-2box`) | `scrim-one` (1 team, 6 boxes, AD domain, 2 injects) |
| worktree / branch | `../tezcatlipoca-run1` / `run1-fresh-20261003` | `../tezcatlipoca-run2` / `run2-scrimone-20261003` |
| run id | `run-dbc07e79` | `run-b6b96a7e` |
| teams × boxes | 2 × (web01 ubuntu, win01 windows) | 1 × (dc01, win02, web01, mail01, db01, splunk01) |
| T0 | 04:45 | 11:41 |
| artifacts | `competitions/scrim-fresh-a/.automated-tests/run-dbc07e79/` (worktree run1) | `competitions/scrim-one/.automated-tests/run-b6b96a7e/` (worktree run2) |

Nine defects were live-found and **fixed on `main` during this session** (each pushed):
state-gate vmid parse (f828e07), harness final-phase check (9ab0a11), blue scoreboard trap +
`TEZ_RED_TEMPLATE` + teardown retry (5b4df92), `TF_VAR_scoring_vm_id` + deploy.log stderr +
scorch fallback (8b4f76f), scrim-report T0/restore-reactions/labels (898763c), windows-foothold
octets (fceb031), fire-test unit selection (851f888), fire-test polling (753f759), prompt-builder
unpack (6d0a753). What follows is what the runs themselves showed, and what should change next.

---

## Run 1 — scrim-fresh-a (2 teams × 2 boxes)

Verdict as generated at teardown: **NOT READY, interaction score 9**. Verdict after regenerating
the report with the metrics fixes (T0 anchor, restore-reactions, labels, windows octets):
**NOT READY, interaction score 21, 4 gates failed** — and every one of the four is explainable
by the comp's shape rather than by agent failure:

| gate | value | threshold | read |
|---|---|---|---|
| max_simultaneous_down | 2 | >= 4 | the comp only HAS 4 boxes; 4 requires killing everything |
| stalls | 1 | == 0 | one genuine 11-min quiet stretch (T+94→T+104) |
| evictions | 0 | >= 1 | blue never expelled red — blue's half of the story |
| injects | 0 | >= 2 | the comp ships no `injects/` dir at all |

What the run actually contained:

- **Red was fast and relentless**: foothold on team2 web01 within seconds of start, Linux root
  at T+4, beacon planted at T+7.5, first takedown at T+23, 22 takedowns total, 7 distinct
  tactics, 80 ok / 4 failed actions, footholds on 3 of 4 boxes plus a Windows cred at one point.
  Critically, **red reacted to every blue restore** — 12 restore-reactions once the metric
  counted them. The original 0 was the metric's bug (see 898763c): red re-killed IIS within
  minutes of each restore, and the old `>=15-min-gap` rule scored continuous pressure as no
  reaction.
- **Red never escalated on Windows** (0 privesc_windows, 0 SYSTEM on win01) despite holding
  credentials to it. Red's impact on win01 was service destruction (stop_disable ×20), not
  control.
- **Blue team2 defended for real**: 6 restorations, TTRs 5–15 min, all four TTRs inside the
  15-min bar. **Blue team1 did not**: 0 restorations, 81 down-minutes, and a 90-minute
  read-only hunt that found nothing and changed nothing. Team1's notebook is honest about why:
  its scoreboard access returned `Forbidden` all run and its local `scorch` oracle printed only
  a header, so it concluded (reasonably) that it could not verify anything and kept its two
  allowed changes in its pocket.
- Both failures were **our bugs, not the agent's**: the cycle prompt taught
  `curl /api/services/$MY_TID` — but `MY_TID` is the subnet identifier (130) and that endpoint
  keys on the engine's internal team id, so it returns `Forbidden` forever (5b4df92); and
  `scorch` derives its probes from pin ports, which catalog-name pins (`"apache"`) don't carry,
  so it printed nothing (8b4f76f adds the `score.py` fallback).
- Injects: **the comp never had any** — authored from a template with no `injects/` dir — so
  the blue inject gate (>= 2) was structurally unpassable. The harness cannot distinguish
  "blue ignored the injects" from "there were no injects" in its verdict text.

## Run 2 — scrim-one (1 team × 6 boxes, AD)

*(numbers to be filled after the run's teardown; the deploy itself surfaced the defects below)*

- Deploy needed one resume (phase-3 ssh timeout to the fresh engine — known flaky first
  contact), then **cleared the phase-4 checkpoint on the fixed gate** — the resume path that
  run 1 proved broken is now proven working.
- verify at pre-T0: logins/injects/isolation/domains/round_loop PASS; **plant_coverage FAIL —
  7 failed nakon steps, all `ad-*` on the team DC** (`ad-blank-password-win`,
  `ad-acl-dcsync-win`, `ad-delegation-win`, …); `degradations: golden root-disk expansion
  failed ×4 — size unmeasurable` (also seen ×1 on run 1).
- The fire test failed three times before passing, exposing three defects (851f888, 753f759):
  unit selection picked `sshd` over `nginx`; the watched scoreboard row was `web01-ssh` (wrong
  row); and single-shot scoreboard reads raced the round cadence. The failed attempts also
  left `sshd` stopped on web01 — recovered with
  `redeploy-competition.py --boxes web01 --mode rollback-ready`, the tool doing exactly what
  it was built for.

---

## Improvement proposals

### tezcatlipoca (pipeline + harness)

1. **P1 — AD misconfig plants fail in the phase-6 repair sweep (pre-promotion).** The phase-6
   sweep applies the machine's full nakon plan, including `ad-*` steps that require a promoted
   DC; phase 7 promotes and plants them properly, but the phase-6 failures are already recorded
   as the machine's plant-coverage tally, so verify's `plant_coverage` gate FAILs a healthy
   post-phase-7 range (run 2: 7 failed steps, all `ad-*`, all on dc01-team130). Fix: exclude
   domain-dependent steps from the phase-6 sweep (phase 7 owns them), and/or let a successful
   phase-7 domain plant supersede the phase-6 failure for that machine in `.deploy_state.json`.
   Owner: `deploy_phases.phase6_repair_sweep` + the coverage record.
2. **P2 — fire-test residue guard.** An aborted fire test (crash between stop and restore)
   leaves a scored service stopped on a live range with no harness to heal it; the next
   `--skip-deploy` attempt then inherits the outage (run 2 needed a manual `rollback-ready`).
   Cheap guard: before stopping anything, the fire test verifies the unit is active and, if
   not, restores it first and logs that it did. Owner: `run-agent-scrim.stage_verify`.
3. **P2 — stop shipping runtime state in tracked comp dirs.** `scrim-one`'s committed
   `.deploy_state.json` (with team passwords and a stale run id) silently redirected the fresh
   deploy's identity/credential resolution until `--from-phase` rewrote it. Proposal: add
   `.deploy_state.json`/`teams.json`/`credentials.txt`/`targets.json` to the tracked-comp-dir
   hygiene test (same family as `test_no_test_artifact_is_tracked`), keep only
   Compfile/boxes/pins/injects tracked. Owner: `tests/test_secret_hygiene.py` + a one-time
   `git rm --cached`.
4. **P3 — scale the rehearsal gates to comp size.** `max_simultaneous_down >= 4` is
   structurally unpassable below 5 boxes; `injects >= 2` is unpassable without an `injects/`
   dir. Gates should derive from the comp (boxes count; inject count published), or the report
   should mark them `n/a (comp shape)` instead of FAIL. Owner: `scrim-report.GATES` +
   `docs/rehearsal-gates.md`.
5. **P3 — comp-aware inject defaults.** An authored comp with no `injects/` dir makes the
   blue inject gate dead weight and halves blue's job. Either ship a minimal inject pair in
   the default authoring template, or have `run-agent-scrim` warn loudly at author time.
   Owner: `stage_author` / `DEFAULT_TEMPLATE`.
6. **P3 — golden-build cost.** Phase 2+4 spent ~42 of the ~55 pre-T0 minutes building
   per-competition engine/golden templates for a 2-box comp (timing sidecar, run 1). Goldens
   are deliberately comp-scoped; a content-addressed golden cache (keyed on the existing
   template hash, not the comp tag) would cut fresh-comp deploys from ~55 to ~20 min. Needs
   the M4 hash discipline re-audited for cross-comp safety before anyone builds it.
   Owner: `deploy_phases.phase4_golden_set`.
7. **P3 — "root-disk expansion failed: size unmeasurable"** recurred on .150 (×1 run 1, ×4
   run 2) and is recorded as a degradation each time. Either the measurable-size probe has a
   real gap on these templates/filesystems, or the check should not degrade the run when the
   disk is provably correctly sized. Owner: the phase-4 expansion step.
8. **done this session** (listed above with commits): state-gate, final-phase check, blue
   prompt trap, scorch fallback, red template default, teardown retry, `TF_VAR_scoring_vm_id`,
   deploy.log stderr, fire test (selection, row, polling), scrim-report metrics.

### bad-auto (red)

1. **Windows escalation coverage.** Run 1 held win01 credentials for 90 minutes and never got
   a Windows foothold (0 `privesc_windows` attempts landed; impact was service-kills only).
   The run's `world.json` + events are archived with run 1's artifacts for a bad-auto-side
   session. Candidate causes: no lateral-move tactic from a Linux root foothold to Windows
   boxes, or `privesc_windows` prerequisites the IIS win01 doesn't meet.
2. **Target rotation under continuous pressure.** 20 of 22 takedowns were IIS re-kills on the
   same two boxes. Red's restore-reaction behavior is now (correctly) scored well, but a
   "after N re-kills of the same target, rotate for a window" pacing rule would convert
   monotonous pressure into breadth (the `max_simultaneous_down` gate wants exactly that).
3. **Destroy stderr.** The transient `badauto destroy rc=1` (run 1 teardown; manual re-run
   succeeded in seconds) had no captured stderr to diagnose. The harness now retries once
   (5b4df92); bad-auto should still write its destroy failures somewhere durable.
   Owner: `badauto` CLI error paths.

### blue (opencode agents)

1. **Run 2 is the A/B for the scoreboard-trap fix.** Run 1's team1 spent 90 minutes read-only
   because its tools lied to it. Run 2's blues get `./score.py` (which works) and injects that
   exist. If run 2's blues still idle with everything UP, the next fix is prompt-side: an
   explicit "if nothing is down, harden something (detection/backup/patching) — idling is not
   defending."
2. **Evidence quality was good and got better.** NOTEBOOK/LOG discipline was solid on both
   teams in run 1 (44 entries); the hand-over format works. Keep the per-cycle notebook-first
   instruction.
3. **Inject workflow.** Run 1 had nothing to submit; run 2 ships two injects with deadlines —
   the `submit-inject` helper and the prompt's fresh-round caveats now get their first real
   exercise at 1-team scale.

## Session state at write time

- `main` = 6d0a753, pushed, 1006 tests green. Both practice worktrees still exist with live
  state: run 1's comp is torn down `--full` (templates removed); run 2's range is mid-event.
- When run 2 finishes: archive both comp dirs' artifacts
  (`python3 test-artifacts.py archive <comp> --all`) **before** discarding the worktrees.
- Estate notes: .193 unreachable all day (its `.env` is the main tree's — retarget before any
  deploy from `main`); scale8's jump VM 2031 still runs on .150 (10.0.0.249).
