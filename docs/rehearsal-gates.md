# Rehearsal gates — numeric pass/fail for a scrim

What a dress rehearsal must hit before the practice counts as ready. `scrim-report.py`
evaluates these mechanically and prints the verdict in `INTERACTION.md`; this doc is
the human-readable contract.

**Tune these numbers after the first rehearsal with them in place, not before.**
A gate nobody has failed yet is a guess.

## The one-line rule

The interaction score — `restorations + red restore-reactions + evictions + injects +
eradication` — is the number the whole harness exists to move. **A run that scores 0 is
a failed run no matter how good red's kill log looks.** Red-vs-empty-room taught nobody
anything; the competition only becomes real when blue's actions change what red does
next and vice versa.

## Red gates

| gate | threshold | why |
|---|---|---|
| takedowns | >= 6 | sustained pressure, not one sweep (round 3: 4) |
| restore-reactions | >= 3 | red must punish blue's restores — the tug-of-war (round 3: 1, by accident) |
| distinct tactics | >= 4 | stop_mask + firewall tamper + cred rotation + persistence (round 3: 3, one pattern) |
| Windows footholds | >= 1 | the DC is the CCDC heart; round 3 never touched it |
| max simultaneous down | >= 4 | of 8 services per team (floor: 2 standing) |
| stalls >= 10 min | == 0 | includes the opening — red attacks within minutes of T0 (round 3: 2, one 29-min opening) |
| evictions | >= 1 | blue actively removing red's access (from red's own health-check tally); observable since 2026-09-23 |

## Blue gates

| gate | threshold | why |
|---|---|---|
| cycles rc=0 | >= 6 | the orchestrator must actually run (round 3: 0 of 40). Tuned 2026-10-12 after two full rehearsals on the GX10 qwen-flash: a blue cycle with real incident work measures 8-10 min (run4: 1 rc=0 + 5 overruns of a 540 s cap; run3: 2 of 2 overruns of a 1800 s cap), so >= 8 in a 60-min event is not reachable at zero timeouts — the gate follows the measured cadence, not the old endpoint's |
| restorations | >= 2 | blue answers red's takedowns |
| time-to-restore | <= 15 min | a restore 40 min later is not a game (rendered since 2026-09-23: `fast of total`) |
| injects | >= 2 | the paperwork layer of CCDC is real points (round 3: 0) |
| notebook entries | >= 10 | working memory, not a write-only diary (round 3: 0) |
| cycle timeouts | == 0 | a timed-out cycle is invisible to `rc=` counts; counted from feed.log TIMEOUT headers since 2026-09-23 |

Scrim-report prints `n/a` for gates whose underlying data the run dir doesn't
have (everything scoreboard-derived needs `scoreboard-state.jsonl`); `n/a` does not
fail the run, the honest red-side numbers carry the verdict.

## Provenance

- Round-3 baseline: `scrim-runs/agent-scrim-2026-09-17c/INTERACTION.md` (score 2 —
  1 inferred restoration, 1 accidental re-kill, 0 injects).

## Fix-coverage checklist (E2E #3, 2026-09-23)

Every fix from rounds 1–3 needs one green check in the e2e run before the practice counts
as READY. A check that cannot run is a finding, not a pass.

- [ ] Fire test heals what it breaks: `fire test: down_detected=True restored=True healed=True`
      in the launch log and web01-http actually answering afterward (run-12 left it down at T0).
- [ ] Red LLM survives the whole event: no `red01 cannot reach the LLM` warning from
      `red_llm_watch` (the dress run's tunnel died mid-event unnoticed); red `llm` events
      `ok=true` with elapsed, ≥1 action/5 min sustained from red's first action.
- [ ] Preflight gates fire for real: template resolution, datastore headroom, catalog check
      all run before terraform apply (new-gate smoke on the cyberfield extreme comp).
- [ ] Plan-archive verification: staged bundle checked engine-side before the plant
      (fail-fast path, not a mid-plant "missing plan archive").
- [ ] Crash drill: mid-phase-5 interrupt → `--from-phase 5` resume shows the
      "Phase-5 re-entry: rolling … back to 'tz-base'" auto-rollback and completes clean.
- [ ] Verifier all-PASS, plus the misconfig-via-guest-agent fallback drill (stop sshd on the
      misconfig target → verify still PASS via agent → restore sshd).
- [ ] Report honesty: INTERACTION.md carries the eviction metric, a Blue interaction section,
      timeout + TTR numbers; a blue-absent run would
      force the NO-INTERACTION verdict.
- [ ] Plant coverage: verify's plant_coverage gate passes on the recorded per-machine coverage (the nakon FAILED tally is informational only).
- [ ] Teardown exit 0, no leftover competition VMIDs (red01's 999 gone).
- [ ] winget/choco Windows fallback observation recorded (known-issues "Windows
      package-manager fallback untested").
