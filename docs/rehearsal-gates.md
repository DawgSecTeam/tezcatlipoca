# Rehearsal gates — numeric pass/fail for a scrim

What a dress rehearsal must hit before the practice counts as ready. `scrim-report.py` evaluates
these mechanically and prints the verdict in `INTERACTION.md`.

**The thresholds live in code, not here**: `scrim_report/gate_table.py`'s `GATES`, with per-run
overrides through `evaluate(thresholds=, gates=)` — a competition's own shape (how many services
are scored, whether SSH is pinned) retunes them, which is why this doc quotes no numbers. What a
threshold cannot express is what a gate means and when it lies.

**Tune these numbers after the first rehearsal with them in place, not before.** A gate nobody
has failed yet is a guess.

## The one-line rule

The interaction score — `restorations + red restore-reactions + evictions + injects +
eradication` — is the number the whole harness exists to move. **A run that scores 0 is a failed
run no matter how good red's kill log looks.** The competition only becomes real when blue's
actions change what red does next and vice versa.

## What each gate means

**Red.** `takedowns` — sustained pressure across the window, not one sweep; service stops only,
since `block_port` is firewall tamper and counts under `distinct_tactics`. Capped at one per scored
service the lineup affords: a hardcoded count above that is passable only through re-kills, which
need blue to restore first. `restore_reactions` — red punishing blue's restores, which is the
tug-of-war itself. `distinct_tactics` — breadth: masking, firewall tamper, credential rotation,
persistence. `windows_footholds` — the DC is the CCDC heart. `max_simultaneous_down` — depth,
floored by the two-standing-services rule and capped at what red may legitimately take down: SSH
is red's own access path and never a target, so a lineup that pins SSH caps below the raw service
count. `stalls` — an idle gap of ten minutes or more, opening included: red attacks within minutes
of T0.

**Blue.** `cycles_rc0` — completed agent cycles per team, a cadence floor and not a target: a
cycle carrying real incident work costs 20–30 min wall clock on the club's endpoint and the two
teams serialise on one shared LLM lock, so a short scrim affords about one per team.
`restorations` — blue answering red's takedowns. `injects` — the paperwork layer, which is real
points, counted only for injects whose window opened inside the run. `notebook_entries` — working
memory, not a write-only diary. `timeouts` — cycles cut at the per-cycle cap
(`TEZ_CYCLE_TIMEOUT`, 600 s) or at the event deadline: one cut per team is expected on a shared
endpoint, several means blue is not finishing real cycles inside its scope — raise the cap or
give the team another endpoint — and `cycles_rc0` still fails a run that never completed a cycle.

`evictions` is deliberately not a gate: red's health checks only see SSH-class footholds, so a zero
is an observability limit rather than a failed red. Blue's self-reported `EVICTED:` lines count
separately as blue activity, never as red evidence. Time-to-restore is reported against
`RESTORE_TTR_GATE_MIN` but does not gate the run on its own.

Per-team LLM endpoints were considered and declined: blue already serialises on one lock per base
URL (`stage_run` builds one lock per distinct endpoint), and an endpoint that cannot take
concurrent load cannot serve an extra team either.

A gate whose underlying data the run dir lacks prints `n/a` and does not fail the run — everything
scoreboard-derived needs `scoreboard-state.jsonl` — so the honest red-side numbers carry the
verdict.
