# Two 90-minute agent scrims on .150 — analysis and improvement proposals (2026-10-03)

Two back-to-back automated scrims ran on node `proxmox` (10.0.0.150; .193 was down all day —
no route), each from its own worktree cut off `main`, each 90 minutes, red and blue both
LLM-driven (openai/gpt-5.6-luna via OpenRouter):

| | run 1 | run 2 |
|---|---|---|
| competition | `scrim-fresh-a` (authored fresh from `same-type-2box`) | `scrim-one` (1 team, 6 boxes, AD domain, injects) |
| worktree / branch | `../tezcatlipoca-run1` / `run1-fresh-20261003` | `../tezcatlipoca-run2` / `run2-scrimone-20261003` |
| run id | `run-dbc07e79` | `run-b6b96a7e` |
| teams × boxes | 2 × (web01 ubuntu, win01 windows) | 1 × (dc01, win02, web01, mail01, db01, splunk01) |
| T0 | 04:45 | 12:16 |
| artifacts | `competitions/scrim-fresh-a/.automated-tests/run-dbc07e79/` (worktree run1) | `competitions/scrim-one/.automated-tests/run-b6b96a7e/` (worktree run2) |

Ten defects were live-found and **fixed on `main` during this session** (each pushed):
state-gate vmid parse (f828e07), harness final-phase check (9ab0a11), blue scoreboard trap +
`TEZ_RED_TEMPLATE` + teardown retry (5b4df92), `TF_VAR_scoring_vm_id` + deploy.log stderr +
scorch fallback (8b4f76f), scrim-report T0/restore-reactions/labels (898763c), windows-foothold
octets (fceb031), fire-test unit selection (851f888), fire-test scoreboard row + polling
(753f759 + 6d0a753), domain-dependent stage split (56ce77e), plant-tally gating (cf88ddc).
What follows is what the runs themselves showed, and what should change next.

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

## Run 2 — scrim-one (1 team × 6 boxes, AD, injects published)

Verdict: **NOT READY, interaction score 33, 2 gates failed** — and both failures are structural
rather than behavioral:

| gate | value | threshold | read |
|---|---|---|---|
| max_simultaneous_down | 2 | >= 4 | red's OWN pacing config caps concurrent downs at 2 until the last 15 min; the gate contradicts the pacing it is configured with |
| evictions | 0 | >= 1 | blue countered red with firewalls and restores, never host-level foothold removal |

What the run contained — and the A/B against run 1 is clean, because the only blue-side changes
between the runs were the scoreboard-trap fixes:

- **Blue went from inert to active.** 9/9 cycles, 3 restorations, **11 of 12 injects
  submitted** (run 1: 0, structurally impossible), 26 notebook entries, and — the biggest
  change — real counter-measures: blue wrote `badauto-block-21` firewall deny rules against
  red's FTP attacks and re-enabled the service behind them. One TTR was 40 min (dc01-ldap
  stayed down from T+63 to capture); the other two were 5 and 15 min.
- **Red gained the Windows dimension** it lacked in run 1: 2 Windows footholds (the stage-split
  fix restored the AD attack surface it would otherwise have lacked — dcsync/delegation/blank-
  password misconfigs planted post-promotion), `beacon_plant_win` among its 7 tactics, 0
  stalls (run 1's genuine 11-min stall absent), 85 ok / 2 failed actions, 25 takedowns,
  17 restore-reactions. The remaining red weakness is breadth: 13 of 25 takedowns were the
  same dc01-ldap re-kill from T+63 to T+85.
- **The pre-event chain is what this run really stress-tested** — the deploy needed a phase-3
  resume, the fire test failed three times (three distinct defects, each fixed), the AD plants
  failed pre-promotion (stage split fixed), the stale tally blocked the restart (tally gating
  fixed), and one aborted attempt left web01's sshd dead until
  `redeploy-competition.py --mode rollback-ready` recovered it. Every failure mode was found
  by a gate that refused to pass silently, which is the system working — but four aborts
  before T0 on a comp that had already deployed once is the session's clearest signal that
  pre-T0 tooling needs one more hardening pass (proposals 2 and 8 below).
- Collection caveat: `RED-TEAM.md` is a stub — red01 was SSH-unreachable at collection time
  (yet alive: the manual destroy killed vmid 999 minutes later). The events/world evidence was
  already safe via the monitor's snapshots.

---

## Improvement proposals

### tezcatlipoca (pipeline + harness)

1. **DONE this session (56ce77e) — AD misconfig plants now plant post-domains.** The phase-6
   repair sweep applied `ad-*`/GPO configs to the unpromoted DC; they failed rc=1 and the
   range shipped without its AD attack surface. `constants.is_domain_dependent` (ad-* prefix +
   GPO/GPP names) routes them to the final stage; a green final pass supersedes the stale
   repair failures in the coverage record, and the merged tally no longer gates when a clean
   record exists (cf88ddc).
2. **P2 — fire-test residue guard.** An aborted fire test (crash between stop and restore)
   leaves a scored service stopped on a live range with no harness to heal it; the next
   `--skip-deploy` attempt then inherits the outage (run 2 needed a manual `rollback-ready`).
   Cheap guard: before stopping anything, the fire test verifies the unit is active and, if
   not, restores it first and logs that it did. Owner: `run-agent-scrim.stage_verify`.
3. **P2 — guard comp dirs against tracked runtime state.** Correction (verified 2026-10-04):
   scrim-one's `.deploy_state.json` was worktree-local, NOT committed — the only tracked
   runtime artifact today is `competitions/same-type-2box/coverage-run1.json`. The failure
   mode this proposal guards (a comp dir shipping state/credentials into git where a later
   deploy reads them as its own) remains worth a cheap structural guard: a hygiene test
   asserting no tracked file under `competitions/*` matches
   `.deploy_state.json`/`teams.json`/`credentials.txt`/`targets.json`/`nakon-config.json`/
   `.frozen.json`/`.template-hashes.json`/`coverage-*.json`, then untrack the one offender.
   Owner: `tests/test_secret_hygiene.py`. (Implementation: 2026-10-04 plan §B.)
4. **P3 — align the rehearsal gates with red's pacing config.** `max_simultaneous_down >= 4`
   contradicts the harness-written pacing (`max_concurrent_down_start: 2` until the last 15
   minutes); either the gate reads the pacing it will be judged against, or the endgame burst
   needs to reliably reach 4 (run 2's endgame did not). Same family as the comp-size scaling
   problem run 1 showed (4-box comp, threshold 4). Owner: `scrim-report.GATES` +
   `docs/rehearsal-gates.md`.
5. **P3 — comp-aware inject defaults.** An authored comp with no `injects/` dir makes the
   blue inject gate dead weight and halves blue's job (run 1). Either ship a minimal inject
   pair in the default authoring template, or have `run-agent-scrim` warn loudly at author
   time. Owner: `stage_author` / `DEFAULT_TEMPLATE`.
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
8. **P2 — red01 goes SSH-dark at endgame.** Run 2's red01 was SSH-unreachable at collection
   time while the VM was alive (events kept flowing to the monitor's snapshot pull minutes
   earlier; the later destroy found it running). Both runs' teardown-time red01 pulls then
   degrade to stubs. Needs a bad-auto-side look (sshd state, routed-segment path, agent
   lifecycle after the endgame burst). Also: the harness's `badauto destroy` failed with
   rc=1 in BOTH runs where the manual re-run succeeded in seconds — the retry (5b4df92)
   covers the transient, but its stderr was swallowed in both; the raise now records it
   (this session) so the next occurrence names its cause.
9. **done this session** (commits inline): state-gate, final-phase check, blue prompt trap,
   scorch fallback, red template default, teardown retry, `TF_VAR_scoring_vm_id`,
   deploy.log stderr, fire test (selection, row, polling), scrim-report metrics (T0,
   restore-reactions, labels, windows octets), domain-dependent stage split, plant-tally
   gating.

### bad-auto (red)

1. **Windows escalation: improved but shallow.** Run 2's red held 2 Windows footholds and ran
   `beacon_plant_win` (run 1: 0 of either — partly the missing AD surface, now fixed). The
   remaining question is depth: no SYSTEM-grade escalation on dc01/win02 was observed, and
   impact stayed service-kills + firewall tampering. Both runs' `world.json` + events are
   archived for a bad-auto-side session.
2. **Target rotation under continuous pressure.** 13 of run 2's 25 takedowns were the same
   dc01-ldap re-kill in a 22-minute window (run 1: 20 of 22 were IIS re-kills). Red's
   restore-reaction behavior is now (correctly) scored well, but a "after N re-kills of the
   same target, rotate for a window" pacing rule would convert monotonous pressure into
   breadth — which is also what the `max_simultaneous_down` gate wants and red's own
   `max_concurrent_down_start: 2` pacing forbids. The two configs should be reconciled.
3. **Destroy stderr + endgame SSH-dark.** The harness's `badauto destroy` failed rc=1 in both
   runs (manual re-run succeeded in seconds); its stderr was swallowed — the raise now records
   it. And run 2's red01 was SSH-unreachable at collection while alive. Both need a bad-auto
   look with the archived evidence.

### blue (opencode agents)

1. **The A/B is conclusive.** Run 1's blues got a lying scoreboard (Forbidden) and a mute
   oracle; run 2's blues got `./score.py` and injects that exist — and went from 0 changes
   (team1, 90 read-only minutes) to 11/12 injects, 3 restorations, and active firewall
   counter-measures against red. The toolchain, not the model, was the bottleneck.
2. **Next blue gap: eviction + the idle case.** Run 2's blue restored services and blocked
   red's tactics at the firewall but never evicted a foothold (evictions 0 in both runs) —
   the prompts never teach the foothold-hunt-and-remove loop against a live adversary.
   And the inverse case stays open: when everything is UP, blue should harden (detection,
   backups, patching) rather than idle.
3. **Evidence quality was good and got better.** NOTEBOOK/LOG discipline solid in both runs;
   the per-cycle notebook-first hand-over works. Keep it.

## Session state at write time

- `main` = 6d0a753, pushed, 1006 tests green. Both practice worktrees still exist with live
  state: run 1's comp is torn down `--full` (templates removed); run 2's range is mid-event.
- When run 2 finishes: archive both comp dirs' artifacts
  (`python3 test-artifacts.py archive <comp> --all`) **before** discarding the worktrees.
- Estate notes: .193 unreachable all day (its `.env` is the main tree's — retarget before any
  deploy from `main`); scale8's jump VM 2031 still runs on .150 (10.0.0.249).
