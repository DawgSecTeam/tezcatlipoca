# Prompt — complete teardown redesign (2026-10-11)

Copy everything below the line into a fresh session.

---

You are redesigning, from scratch, every teardown path in this project. The
owner's verdict on the current state: **the teardown is baroque because it was
never architected — it is scar tissue from incidents, accreted one guard and
one flag at a time.** You have a mandate to replace the mechanism entirely,
in a DRY, object-oriented design. This is a pre-approved architectural change;
you do not need permission for the redesign itself.

The project's first discipline is in AGENTS.md: fix causes, not symptoms;
complexity belongs in the tool, not the operator; anything a doc tells an
agent to "remember" is a bug filed against the code. That standard applies
double here — the current teardown survives only because operators memorize
its exceptions.

## What exists today (verify, don't trust this list)

Teardown logic is smeared across at least:

- `destroy-competition.py` — the CLI driver (resumable, stale-lock recovery,
  artifact collection before destroy, flag forest: `--full`,
  `--end-of-competition`, `--legacy-tags`, `--allow-untagged`,
  `--skip-artifacts`, `--yes`)
- `destroy_gate_ops.py`, `destroy_sweep_ops.py`, `destroy_templates_ops.py`,
  `vm_ownership.py` — ownership/tag gates and the leftover sweep, as four
  loosely-coupled modules
- `deploy_lib/phases/cleanup.py` — phase-1 destroy waves (per hosting node,
  per slot, golden spans)
- `scrim/teardown_stage.py` + `scrim/red_link.py` — the harness's teardown
  stage and red-evidence collection
- `red_plant_ops.destroy_red` — red01, which shells out to **bad-auto's**
  `badauto destroy` — a SECOND, parallel ownership system (its own deploy
  stamp, its own VM description marker, its own `--force`/`--skip-vm`
  escapes) in the sibling repo `~/dev/dawgsec/bad-auto`
- terraform destroy paths, the reconcile rm+import fixups, and the flock in
  `~/.tezcatlipoca/locks/`

Read all of it, plus the tests that pin it, before designing anything. The
incident history is in the git log of these files and in AGENTS.md — the
guards you will delete were each added for a reason. Catalog every reason
first; the redesign must re-earn each guarantee, not drop it.

## Non-negotiable invariants (the new design preserves these, better)

These are the guarantees the current scar tissue provides. Your object model
must provide all of them as FIRST-CLASS properties, not as flags:

1. **Ownership is total and verified.** Everything a deploy creates carries
   that deploy's full ownership set (comp id + per-run `run-<id>` tags +
   clone markers). Teardown destroys ONLY what its ownership set names.
   A state file without a run id is refused. Unmarked/foreign VMs are
   never touched — skip-and-report, never sweep. (2026-09-30: an over-broad
   sweep destroyed two OTHER competitions' engines and goldens.)
2. **Force by default, but only against owned targets.** Teardowns
   force-stop; no clean ACPI shutdowns (owner directive 2026-10-01).
3. **Evidence before destruction.** Reports, red01's world.json, blue logs,
   and engine captures are collected into the run dir BEFORE anything is
   destroyed; a dead box warns and proceeds, never blocks, and what could
   not be collected is recorded, not silently dropped.
4. **Resumable and idempotent.** Re-running teardown until it exits clean is
   the contract — stale locks recover, half-destroyed states re-enter,
   nothing requires a flag to resume.
5. **Concurrent-safe.** Two ranges on one node (isolated worktrees, distinct
   ownership sets) can tear down simultaneously without touching each other.
6. **One refusal vocabulary.** Every "I will not destroy this" verdict names
   what it saw, what it expected, and what would change its mind — the same
   vocabulary at every layer, including bad-auto's.
7. **Teardown exit 0 means the estate is clean** for this ownership set:
   nothing left running, nothing left tagged, locks released, evidence
   archived.

## Process

1. **Pin the current behavior first.** Write characterization tests that
   assert the invariants above against the CURRENT code (they pass today,
   and must keep passing under the new design). These tests are the
   redesign's safety net and its acceptance harness.
2. **Write the design before the code.** A short RFC (target
   `docs/teardown-redesign.md`): the object model (ownership, inventory,
   plan, waves, execution, evidence, report — your names), how each
   invariant maps to a type/contract rather than a runtime check, what gets
   deleted, and the bad-auto boundary (shared ownership CONTRACT; its
   internals are redesigned only as far as the contract requires).
3. **Implement incrementally, suite green at every commit.** No long-lived
   dual paths: entry points become thin CLI shims over the new model as they
   migrate, and the old modules are DELETED in the same change that stops
   calling them. Escape hatches (`--force`, `--legacy-tags`,
   `--allow-untagged`, `--skip-vm`) die with their code paths — if one has a
   genuine survivor (e.g. legacy comps), it becomes an explicit, logged,
   named operation in the new model, not a boolean that loosens matching.
4. **Live-prove from a worktree** (practice-run rule: new worktree off main,
   own `--scoring-vmid` + team identifiers + mgmt IP block). Prove at least:
   a clean deploy → destroy cycle with zero flags and zero warnings; a
   mid-teardown kill → resume to exit 0; a foreign/untagged VM in the blast
   radius → refused and reported; evidence collection before destruction;
   and (if a second node or session is available) concurrent coexistence.
   Teardown collection-vs-destroy ordering is exercised for real, not
   mocked.
5. **Drain the docs.** Every teardown trap recipe that existed to route
   around the old design (AGENTS.md teardown sections, usage-agents.md,
   known-issues.md entries) is rewritten or deleted — the writepad rule
   applies. The redesign is not done while a doc still teaches the old
   jank.

## Decision authority

- Object model, module layout, naming, APIs, deletion order: yours. Decide
  and record the reasoning.
- The SEMANTICS of what may be destroyed (ownership rules, refusal
  conditions, tag schema): preserve exactly. Changing them is an escalation
  to the owner.
- If mid-design you conclude part of the current mechanism is actually right
  (e.g. terraform stays the destroy executor for terraform-managed state) —
  keeping it is fine; say why. Escalate only for true forks: replacing the
  terraform layer itself, or changing what a competition's lifecycle IS.

## Coordination

- Work on a branch + worktree. A test-loop session may be running in main's
  tree with a live range: never destroy or pause anything you did not create;
  merge to main between its runs, after your suite and live proof are green.
- The estate is shared (~107 VMs on the node). Your validation comp uses
  identifiers that collide with nothing live; preflight must pass clean.

## Deliverables

1. `docs/teardown-redesign.md` — the RFC, updated to describe what SHIPPED.
2. The new implementation + characterization tests migrated and green,
   everything else deleted (report the net line count and the list of
   removed flags/modules).
3. One teardown entry point per surface (comp / harness / red01), all thin
   shims over the same model, bad-auto speaking the same ownership contract.
4. Live-validation transcript (commands + exits, in the RFC or a report).
5. Docs drained; the standing test-run guide
   (`competitions/scrim-one/test-run-loop.md`) untouched or shorter.

Definition of done: a fresh deploy torn down unattended, exit 0, no flags
beyond `--competition`/`--yes`, zero degradations, evidence archived, docs
consistent — and every characterization test from step 1 passing against the
new code.
