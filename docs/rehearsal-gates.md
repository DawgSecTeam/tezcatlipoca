# Rehearsal gates — numeric pass/fail for a scrim

What a dress rehearsal must hit before the practice counts as ready. `scrim-report.py` evaluates
these mechanically and prints the verdict in `INTERACTION.md`; this is the human-readable contract.

**Tune these numbers after the first rehearsal with them in place, not before.** A gate nobody
has failed yet is a guess.

## The one-line rule

The interaction score — `restorations + red restore-reactions + evictions + injects +
eradication` — is the number the whole harness exists to move. **A run that scores 0 is a
failed run no matter how good red's kill log looks.** The competition only becomes real when
blue's actions change what red does next and vice versa.

## Red gates

| Gate | Threshold | Why |
|---|---|---|
| Takedowns | ≥ 6 | Sustained pressure, not one sweep. Service stops only — `block_port` is firewall tamper and counts under distinct tactics |
| Restore-reactions | ≥ 3 | Red must punish blue's restores — the tug-of-war |
| Distinct tactics | ≥ 4 | stop_mask + firewall tamper + cred rotation + persistence |
| Windows footholds | ≥ 1 | The DC is the CCDC heart |
| Max simultaneous down | ≥ 4 | Of 8 services per team (floor: 2 standing). Capped at the scored services red may take down: SSH is red's own access path and is never a target, so `scrim-one-light` (two SSH pins) caps at 3 |
| Stalls ≥ 10 min | 0 | Includes the opening — red attacks within minutes of T0 |

Evictions are **not** a red gate: red's health checks only see SSH-class footholds, so a zero
eviction count is an observability limit, not a failed red. Blue's self-reported `EVICTED:` lines
are not used as red evidence (the report counts them separately, as blue activity).

## Blue gates

Numbers recalibrated from the scrim campaigns; the measured basis is in each row. The red thresholds were not recalibrated — the same campaigns give no measured endpoint-side bound on them.

| Gate | Threshold | Why |
|---|---|---|
| Cycles rc=0 | ≥ 2 | One completed cycle per blue team. Measured: a cycle with real incident work costs 20–30 min wall clock on a mid-size local model (a trivial completion took 11.5 min) and the two teams serialise on one shared LLM lock, so a 30-minute scrim affords about one cycle per team. Measured basis: 4 cycles in 45 min, 3 in 30 min, 1 rc=0 in 30 min, 0 in 25 min. Recalibrated from ≥ 6. A cadence floor for `--teams 2`, not a target |
| Restorations | ≥ 2 | Blue answers red's takedowns. **Not recalibrated**: all four scrims logged 0 restorations, so nothing measures blue's restore capacity — lowering it would launder the measurement gap into a pass |
| Time-to-restore | ≤ 15 min | A restore 40 min later is not a game. **Not recalibrated**: no run has produced a restoration to time |
| Injects | ≥ 2 | The paperwork layer of CCDC is real points. Capped at the injects whose window opened inside the run (an inject that opens after the run ends cannot be submitted; `run-0637d642`: 4 published, 1 open in 20 minutes). **Not recalibrated** otherwise: measured 0, 1, and 2 in three scrim runs — one run met it, so the cadence does not bound it |
| Notebook entries | ≥ 10 | Working memory, not a write-only diary. **Not recalibrated**: measured 0 and 2, both in windows where blue completed at most one cycle; a single cycle can write many lines, so no measured ceiling |
| Cycle timeouts | ≤ 2 | One window-edge cut per blue team: with 20–30 min cycles and the 25–45 min scrims this club runs, a cycle started near the end is always cut at the event deadline (and is logged as a TIMEOUT, since the deadline is now authoritative). Measured 2 in a 25-minute scrim, 1 in a 30-minute one. Recalibrated from 0; three or more means the endpoint stopped producing, and `Cycles rc=0` still fails a run that never completed a cycle |

Time-to-restore is reported against `RESTORE_TTR_GATE_MIN` (15 min) but is not in `GATES`: the
report prints it under Blue, and it does not by itself fail the run.

**Per-team LLM endpoints were considered and declined.** Splitting blue across per-team endpoints would only raise the cadence if the endpoints could take it: blue still serialises on one shared lock per base URL (`stage_run` builds one lock per distinct endpoint, and concurrent opencode sessions on one endpoint are what the lock exists to prevent), and an endpoint that cannot take concurrent load cannot serve an extra team. A split that leaves the lock in place changes nothing, so the numbers above are calibrated to the serialised cadence.

`scrim-report.py` prints `n/a` for a gate whose underlying data the run dir doesn't have
(everything scoreboard-derived needs `scoreboard-state.jsonl`); `n/a` does not fail the run, and
the honest red-side numbers carry the verdict.
