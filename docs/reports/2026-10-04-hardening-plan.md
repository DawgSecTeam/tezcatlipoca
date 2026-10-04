# Hardening plan — implement the 2026-10-03 scrim findings, validate in a new scrim (2026-10-04)

Turns every open proposal in
[2026-10-03-scrim-double-analysis.md](2026-10-03-scrim-double-analysis.md) into a concrete
change, and defines the scrim that proves them. Working rule from that session still applies:
every item ships with tests, everything merges to `main` before the validation scrim starts,
and the scrim runs from a NEW worktree cut off `main` (AGENTS.md practice-run rule).

**Acceptance for the whole plan** — one 90-minute scrim on `scrim-one` that:
reaches T0 with zero harness aborts, verifies clean on the first pass, ends with red rotating
targets and reaching its pacing cap, blue evicting at least one foothold, and a teardown that
either runs clean or names its failure. Baseline to beat: run 2's score 33 / 2 gate fails.

---

## A. Pre-T0 hardening (harness) — "no more four aborts before T0"

**A1. Fire-test residue guard** (analysis §tezcatlipoca 2). Before the fire test stops
anything, it must prove the unit is active — and restore it first if a previous aborted run
left it dead (run 2 lost web01's sshd this way and needed a manual `rollback-ready`).

- `run-agent-scrim.py`: extract the fire-test body from `stage_verify` into
  `run_fire_test(args, comp, creds, sudo_web01, ssh_web01)` (the closures already exist —
  pass them in). First step: `systemctl is-active <unit>`; if not active, run the existing
  unmask+start pair, poll `is-active` + the scoreboard row until green (bounded, ~3 min),
  and log `fire test: pre-existing outage on <unit> — restored before testing`.
- Tests: with the runners injected, assert (a) active unit → stop proceeds immediately;
  (b) dead unit → restore attempted before stop; (c) restore that never comes up aborts
  before the stop (fail closed before T0, cheap — the session's own lesson).

**A2. `badauto destroy` stderr, end to end.** Done for the raise (d600114); extend it: when
the retry also fails, teardown writes `bad-auto-destroy.log` (stdout+stderr) into the run's
test folder before raising, so the evidence survives the crash. Test: fake runner returns
rc=1 twice → file exists in test dir with stderr tail.

## B. Comp-dir hygiene + authoring (harness)

**B1. Correct the analysis doc.** Its proposal 3 claimed scrim-one shipped a committed
`.deploy_state.json`; verified false — only `competitions/same-type-2box/coverage-run1.json`
is tracked (a runtime artifact). The failure mode the proposal guards against is real
(registrable future mistake), the premise wasn't. Fix the doc in this same commit.

**B2. Hygiene test + untrack.** New check in `tests/test_secret_hygiene.py` (family of
`test_no_test_artifact_is_tracked`): no tracked file under `competitions/*` may match
`.deploy_state.json`, `teams.json`, `credentials.txt`, `targets.json`, `nakon-config.json`,
`.frozen.json`, `.template-hashes.json`, `coverage-*.json`. Then `git rm --cached
competitions/same-type-2box/coverage-run1.json`.

**B3. Author-time inject warning + `DEFAULT_TEMPLATE` repair.** `DEFAULT_TEMPLATE` points at
`competitions/agent-scrim-2026-09-17b`, which does not exist — `--new` without
`--from-template` crashes with a raw PathError. Make `stage_author` (a) fail with the list of
valid templates when neither the default nor `--from-template` resolves, and (b) warn loudly
(`no injects/ in template — blue's inject work will be structurally impossible`) when the
authored comp has no `injects/`. Decision recorded: we do NOT ship canned injects in the
template — inject text is scenario-specific and canned injects would teach blue to ignore
them. Tests: author from a template without injects → warning present; bad default → error
names valid templates.

## C. Gates that measure the right things (scrim-report + harness)

**C1. Pacing-aware `max_simultaneous_down`.** The gate demands ≥4 while the harness paces red
at `max_concurrent_down_start: 2` until the final 15 minutes. `record_phase` gains the three
pacing values the harness already computes (`max_concurrent_down_start/end/endgame`) —
`scrim-report` reads `run.json`'s pacing and sets the threshold to the **end** value (4 today,
comp-size-independent), falling back to 4 when absent (legacy run dirs, EXPECTED_17C
unchanged). Also surface `start` in the gate table so a mid-run snapshot isn't misread.

**C2. `injects` gate is comp-aware.** Gate input gains "injects published at capture" (already
in the final scoreboard evidence). 0 published → the gate reports `n/a (comp ships none)`
and drops out of the score instead of failing blue for the authoring choice. ≥1 published →
threshold stays 2, but capped at the published count (submitting 11 of 12 must not read as
failing 2 if only 2 existed).

**C3. Takedown-spread observation.** New informational metric (not a gate yet): largest single
target's share of takedowns. Run 1: 91%, run 2: 52% — the number that justifies red's
rotation work (F1) and lets us see it land without hard-coding a threshold prematurely.
Tests: fixture run dirs for both shapes; EXPECTED_17C additions.

## D. Root-disk probe retry (golden_ops)

The grow retries 3× across the boot window, but the size probe runs **once**, immediately
after the last failure — the same boot-window instability that broke the grow breaks the
probe, and the run records a permanent "size unmeasurable" degradation (×1 run 1, ×4 run 2).
Fix: give the probe the same retry budget (3× / 15 s) inside the existing failure branch; only
record the degradation when the probe still cannot measure after the window. If it CAN measure
and the disk is big enough, log `expansion failed but root is <N>G ≥ <need>G — continuing` and
record NO degradation. Tests: inject `ssh_via_gateway` (the seam exists — `ctx`) for
probe-fails-then-succeeds, probe-says-big-enough, probe-says-too-small (raises, unchanged).

## E. Blue: eviction + the idle case (harness prompts)

**E1. Eviction guidance.** The cycle prompt's CYCLE TASK section gains one numbered step: when
the scoreboard is green, spend one of the two allowed changes hunting red's persistence —
unknown services, new scheduled tasks/Run keys, unfamiliar accounts, live sessions — and when
something is found, remove it and note it in LOG.md (`EVICTED:` line so the report can count
it). Run 1+2: evictions 0 in both; blue restored and firewalled but never expelled.

**E2. Idle-case instruction.** Explicit: "if nothing is DOWN and no inject is due, harden
something real (backups, detection, patching, account hygiene) — idling is not defending."
Tests: extend `BluePrompt` pins for both instructions (the prompt is generated text — pin
substrings).

## F. Red: rotation + endgame (bad-auto — sibling repo, own branch + its 329-test suite)

**F1. Rotation rule.** In the director's target selection: after N re-kills of the same
target within W minutes (start N=3, W=20), demote that target for a cooldown window and pick
the next-healthiest. Bad-auto-side unit tests; measured in the scrim by C3's spread metric
(target: no single target > 40% of takedowns).

**F2. Endgame breadth.** Run 2's endgame never reached `max_concurrent_down_end` (4). Review
`director.py`'s endgame branch against the archived run-2 `world.json`/events (archived under
`~/.tezcatlipoca/automated-tests/scrim-one/run-b6b96a7e/`): find why the burst didn't fire —
hypothesis: `endgame_force_active` forces DECISIONS but not new TARGETS, and red had
already boxed itself into one target by rotation-blindness. Fix + unit tests; measured by C1's
gate actually passing.

**F3. Destroy diagnostics.** `badauto destroy` writes its own failure log (rc + stderr tail)
under its state dir, so the harness-side capture (A2) is never the only copy. Also instrument:
the destroy path logs which sub-step failed (beacon-stop / NAT removal / VM destroy) — the
in-harness rc=1 happened twice with zero diagnostics, manual re-run always succeeded.

**F4. red01 endgame SSH-dark.** Run 2's red01 was SSH-unreachable at collection while alive.
Investigation task with the archived evidence: red01's last event ts vs collection ts, sshd
unit state in the events, routed-segment path. Outcome is a diagnosis first; the fix
(sshd watchdog on red01, or collection fallback to the controller channel) follows it. Do not
blind-code this one.

## G. Content-addressed golden cache (harness — biggest risk, flag-gated)

**G0. Design spike first** (one doc, no code): how shared goldens square with
(a) run-id ownership tags — a cached golden belongs to no run; propose a `golden-cache` tag
exempt from phase-1 sweeps and `--full` teardowns unless `--purge-golden-cache`;
(b) vmid allocation — the golden block sits at `<scoring-vmid>+150` per comp; a cache hit must
either re-tag the existing VM into this comp's block (rename/resize) or teach later phases to
address goldens by lookup instead of computed vmid;
(c) the M4 hash gate — hash already content-addresses the build; the cache key is
`hash + node`; (d) frozen comps. The spike ends with a go/no-go; **no-go leaves item 6 of the
analysis open and everything else proceeds** — it is the only item with this exit.

**G1. If go:** implementation behind `TEZ_GOLDEN_CACHE=1` (default off), phase 4 looks up
`golden-<hash8>` before building. Tests: lookup hit/miss/hash-mismatch offline with a faked
PVE layer; ownership-tag exemption tests in the destroy suite.

**G2. Validation:** deploy-only (not part of the scrim event): deploy a throwaway comp with
the flag on (phase 4 builds + records), destroy teams-only, deploy a same-lineup comp — phase
4 must skip the builds, target < 5 min vs run 1's ~25. Measure with the timing sidecar.

## H. Sequencing

| order | item | size | gate to proceed |
|---|---|---|---|
| 1 | A1, A2, B1–B3, C1–C3, D, E1–E2 | ~1 day | tests green on `main` |
| 2 | F1–F3 (bad-auto branch) | ~1 day | bad-auto suite green; land before the scrim |
| 3 | F4 investigation (no code) | ~2 h | diagnosis written into the analysis doc |
| 4 | G0 spike | ~2 h | go/no-go decision |
| 5 | G1–G2 (only on go) | ~1 day | flag-off by default; scrim runs flag-on only if G2 passes |
| 6 | validation scrim | ~4 h wall clock | acceptance table below |

## The validation scrim

Same recipe as 2026-10-03 (each trap already documented there): new worktree off `main`;
`.env.cyberrange-20260930` (+ `TF_VAR_team_identifiers=130,131`,
`TEZ_RED_TEMPLATE=base-ubuntu24.04-fix` — now settable via env rather than flag);
re-check .193 reachability first in case it came back. G2's cache timing measurement runs
immediately before the event; then the event itself is `scrim-one`, 1 team, 90 min — the
richest comp we have (AD + injects) and the direct baseline comparison.

| # | acceptance gate | source |
|---|---|---|
| 1 | T0 on the first harness invocation — zero aborts, fire test passes first try | run log |
| 2 | verify `plant_coverage` PASS on the first post-deploy verify | verify SUMMARY |
| 3 | `max_simultaneous_down >= 4` (pacing-aware threshold) PASS | INTERACTION gates |
| 4 | red spread: no single target > 40% of takedowns (C3 metric) | INTERACTION |
| 5 | `evictions >= 1` PASS | INTERACTION gates |
| 6 | injects submitted >= min(2, published) | INTERACTION gates |
| 7 | teardown completes end-to-end; if `badauto destroy` fails, `bad-auto-destroy.log` names the sub-step | run log + A2 artifact |
| 8 | `RED-TEAM.md` present (endgame SSH-dark fixed or collection fallback works) | collection.json |
| 9 | score >= baseline 33 against the SAME gates run 2 was judged on (re-score run 2's artifacts with the new scrim-report for comparability) | scrim-report |
| 10 | golden cache (only if G went): phase-4 wall time on the scrim deploy < 5 min (cache hit from G2's comp) | timing sidecar |

Score comparability note: C1/C2 change thresholds, so the raw scrim-report score is not
directly comparable to run 2's 33 — gate 9 therefore re-scores run 2's archived artifacts with
the new code and compares like-for-like. If any acceptance gate fails, the failure is
triaged into the analysis doc the same day, per the standing rule that fixed issues leave
`docs/known-issues.md` and git history is the record.
