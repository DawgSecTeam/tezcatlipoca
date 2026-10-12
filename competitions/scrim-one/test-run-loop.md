# Test-run loop — scrim-one

The goal: this stack runs a full competition end-to-end with no human and no
agent intervention. A test run exists to find defects so they can be killed at
the root. The deploying agent starts runs, watches them, and reports — it does
not live-patch. If a run ever needs a hand-applied workaround, that workaround
is the defect.

Copy everything below the line into a fresh session.

---

## The rules

1. **Fix before running.** If anything is known-broken — a failed preflight, a
   red test suite, a degradation in the last deploy's state, an entry on the
   open list that this run would hit — fix it first, in code, with a test.
   There is no point running a full competition on a foundation that is
   already broken. A run is the instrument for finding *unknown* defects, not
   for re-measuring known ones.
2. **Diagnose before retrying.** When anything fails, read its evidence and
   name the cause before any retry. If the failure left no evidence, fixing
   that is part of the fix — a pipeline that destroys its own failure logs
   guarantees the failure repeats. Never pattern-match a failure to "known
   flake" and resume; flaky means broken.
3. **Fix causes, not symptoms, in code.** Every fix lands with a regression
   test and a push before the next run starts. Fixes may cross layers: if the
   elegant fix touches parts the failure did not surface in, make that fix.
   "It passed this time" is not a fix.
4. **No workarounds during a run.** No manual SSH repairs, hand-edited VMs,
   hand-cleared flags, `--force`, or ad-hoc scripts. If a workaround feels
   necessary, stop the loop: the thing that made it feel necessary is the bug.
   Removing a workaround — and its trap documentation — is part of the fix.
5. **Runs continue until clean, however long that takes.** One cycle =
   reset → full run (deploy → verify → event → grade) → grade against
   `docs/rehearsal-gates.md` → archive evidence. Defect-free means: teardown
   exits clean, deploy has zero degradations, verify PASS, every gate with
   data passes. Then fix-and-loop ends; anything less loops again. There is
   no iteration cap.
6. **Decide, then explain.** You decide everything else yourself — make the
   call, record the reasoning in the commit or the report, keep moving. Raise
   to the user only: architectural redesigns, changes to what a competition
   is (scoring, ROE, gates), anything that could touch another competition's
   infrastructure, and blockers that need a decision only the user can make.
   Never stall waiting for permission you already have.
7. **known-issues.md is a writepad, not a ledger.** Every entry is open work
   with a concrete next action. Fixed issues leave it immediately (git
   history is the archive); a workaround may be recorded only next to the
   issue that will remove it. Drain it at the start of every cycle; an entry
   that survives two cycles without movement is either fixed or raised.
8. **Report honestly, with numbers.** At every run boundary — started, failed
   (with the diagnosed cause), graded (per-gate numbers, deltas, what is
   open). A blocker report naming the unresolved cause is a valid outcome; a
   green report that hides a tolerated failure is not.

**An issue is GONE when all four hold:** (1) its root cause is fixed in code,
not in procedure; (2) a regression test fails without the fix and passes with
it; (3) any workaround — and the trap documentation that routed around it —
is deleted; (4) one full cycle completes without it recurring. "Passed this
time", tolerated, deferred, and documented-around are not gone.

## The cycle

```bash
# 1. Reset — must exit clean on its own; a teardown that needs coaxing is an issue
.venv/bin/python destroy-competition.py --competition scrim-one --yes --full --end-of-competition

# 2. Run — detached, start to finish, no intervention
setsid nohup .venv/bin/python -u run-agent-scrim.py --competition scrim-one \
  --teams 1 --duration-min 60 --seed-depth 3 --blue-watchdog \
  --llm-base-url http://100.64.0.19:8000/v1 --red-model qwen3.8-flash-next \
  --blue-base-url http://100.64.0.19:8000/v1 --blue-model qwen3.8-flash-next \
  --red-ip 10.0.0.198 --red-gw 10.0.0.1 --red-storage hdd \
  > logs/scrim-$(date +%Y%m%d-%H%M%S).log 2>&1 &

# 3. Grade the run dir (competitions/scrim-one/.automated-tests/<run-id>/):
#    deploy tail, .deploy_state.json degradations, INTERACTION.md, test.json,
#    docs/rehearsal-gates.md
# 4. Archive before the next teardown
.venv/bin/python test-artifacts.py archive scrim-one --all
```

Comp-specific ground truth (endpoints, vmids, node) lives in
[deploy-prompt.md](deploy-prompt.md) and `docs/environment-facts.md` — not
here. If this guide and the code disagree, the code wins and the guide is
wrong.

## Standing rule

Anything this guide tells an agent to "remember" or "watch out for" is a bug
filed against the code: the tool should have done it itself. Every fix should
make this guide shorter.
