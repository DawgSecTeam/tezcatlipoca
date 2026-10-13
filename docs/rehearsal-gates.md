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
| Takedowns | ≥ 6 | Sustained pressure, not one sweep |
| Restore-reactions | ≥ 3 | Red must punish blue's restores — the tug-of-war |
| Distinct tactics | ≥ 4 | stop_mask + firewall tamper + cred rotation + persistence |
| Windows footholds | ≥ 1 | The DC is the CCDC heart |
| Max simultaneous down | ≥ 4 | Of 8 services per team (floor: 2 standing) |
| Stalls ≥ 10 min | 0 | Includes the opening — red attacks within minutes of T0 |
| Evictions | ≥ 1 | Blue actively removing red's access (from red's own health-check tally) |

## Blue gates

| Gate | Threshold | Why |
|---|---|---|
| Cycles rc=0 | ≥ 6 | The orchestrator must actually run; the gate follows measured cadence (a cycle with real incident work runs 8–10 min), not the raw endpoint capacity |
| Restorations | ≥ 2 | Blue answers red's takedowns |
| Time-to-restore | ≤ 15 min | A restore 40 min later is not a game |
| Injects | ≥ 2 | The paperwork layer of CCDC is real points |
| Notebook entries | ≥ 10 | Working memory, not a write-only diary |
| Cycle timeouts | 0 | A timed-out cycle is invisible to `rc=` counts; counted from `feed.log` TIMEOUT headers |

`scrim-report.py` prints `n/a` for a gate whose underlying data the run dir doesn't have
(everything scoreboard-derived needs `scoreboard-state.jsonl`); `n/a` does not fail the run, and
the honest red-side numbers carry the verdict.
