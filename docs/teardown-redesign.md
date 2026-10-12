# Teardown redesign — RFC

Status: **v1 design** (updated to as-shipped when the work lands). Branch
`teardown-redesign`, worktree `../tezcatlipoca-teardown-redesign`. Pre-approved
architectural change (owner mandate 2026-10-11): replace the accreted teardown
mechanism with a DRY, object-oriented design that re-earns every guarantee the
scar tissue provides.

## 1. The verdict, restated precisely

Teardown survives on operator memory, not on its design. Reading all of it:

- **Three parallel sequencings of the same operation.**
  `destroy-competition.py` (the standalone teardown),
  `deploy_lib/phases/cleanup.py` (phase 1 destroying the PRIOR run), and
  `scrim/teardown_stage.py` (the harness stage) each hand-roll their own step
  order, their own warn-and-proceed try/except forest, and their own partial
  view of ownership.
- **Ordering invariants live in comments, not types.** "Evidence before
  destruction" is enforced by the reader's care (destroy-competition.py:188-195,
  teardown_stage.py:147-155); nothing stops a mode added later from reordering it.
- **A ad-hoc refusal vocabulary.** Refusals are `RuntimeError` prose,
  `SystemExit` prose, or `print` + `sys.exit(1)` — three shapes, no contract,
  untestable as a class.
- **No exit contract.** Exit 0 means "the code finished", not "the estate is
  clean for this ownership set"; the scale8 leak (red01 alive after DONE) and
  the missing-tfstate silent no-op are both this gap.
- **Flags that loosen matching.** `--legacy-tags`, `--allow-untagged`,
  `--skip-vm`, `TEZ_ALLOW_UNTAGGED_RECLAIM` existed as booleans that widened the
  blast radius. All four are already dead in code (removed 0.2.0 and 3a00ee6);
  the redesign deletes their doc ghosts too. `--skip-artifacts` dies with this
  redesign (the journal makes it pointless).

What is already RIGHT and stays: `vm_ownership.ownership_verdict` is the single
ownership proof shared by preflight and every destroy path (internals.md's
"one definition everywhere"); terraform is the right executor for
terraform-managed state; the bounded-retry destroy loop with recovery between
attempts; the flock-liveness warning; the clone-marker-in-POST trick.

## 2. Incident catalog — the "must re-earn" list

Every guard below exists because of a named incident. The redesign must provide
each guarantee as a first-class property. (Git-archaeology detail in the commit
messages of the files listed; the headline entries:)

| Date | Incident | Guarantee to re-earn | Where it lands in the new model |
|---|---|---|---|
| 2026-09-17 | interactive picker blocked scripted teardown | scriptable: `--competition NAME --yes` | CLI shim keeps both flags |
| 2026-09-23 | out-of-band-deleted template left a half-done teardown | destroy retries; idempotent re-run | engine resumability + bounded terraform loop |
| 2026-09-23 | clone purge matched a vmid now hosting something else | purge predicate = vmid AND identity | Ownership required by every delete |
| 2026-09-24 | concurrent comps shared vmids/bridges | per-comp state + collision gates | Inventory + per-comp state dir (unchanged) |
| 2026-09-25 | mid-clone kill left an untagged VM preflight called foreign | ownership evidence from the clone's first instant | clone marker stays; MARKER verdict stays |
| 2026-09-30 | **over-broad sweep destroyed 10 foreign VMs** (ad-hoc script bypassed the guard) | full-tag-set-only sweep; guarded path is the ONLY ergonomic path; never sweep by name | `sweep_leftovers` takes an Ownership, matches `expect ⊆ tags`; no name predicates exist |
| 2026-09-30 | graceful ACPI shutdown burned minutes and hung on agent-down DCs | force-stop everything owned, no clean shutdowns (owner directive) | PreStopStep + delete primitive use `status/stop` only |
| 2026-10-02 | smoke vs live-2box: same comp id, two worktrees, indistinguishable | per-deploy run id; full set required everywhere | Ownership(run_id) anchor; no-run-id state refused |
| 2026-10-02 | clean-for-template landed on a live FOREIGN engine (lost ssh host keys) | identity assert before the most destructive command; flock liveness warning | engine-build identity + `other_deploys_in_flight` stay (deploy-side, untouched) |
| 2026-10-02 | terraform destroy hung forever on a qm lock (agent-down DC) | bounded per-attempt timeout; timeout reaches recovery | TerraformStep keeps 4×1800s + lock-clear + sweep between attempts |
| 2026-10-02 | torn `.deploy_state.json` write; world-readable passwords | atomic single-writer state | config_ops.write_state stays; engine reads through it |
| 2026-10-04 | sanctioned concurrent deploy queued 90 min behind a held lock | concurrency = warn (liveness cannot lie), identity = refuse | flock warning stays a warning; ownership stays a refusal |
| 2026-10-04 | destroy failures rc=1 with zero diagnostics | failure evidence survives the crash | bad-auto-destroy.log + destroy-failure.log contract stays; engine journals every step |
| 2026-10-05 | missing tfstate: destroy "succeeded" against nothing | refuse when state gone but last_phase ≥ 2 | tfstate gate in the engine's gate phase |
| 2026-10-03/08 | red01 survived the range (scale8); identity guards needed env plumbing | red01 destroyed with the range; env contract explicit | RedStep owns the badauto call + BAuto_COMPETITION_DIR |
| 2026-10-09 | report raced the final red pull (graded a world that didn't exist yet) | evidence ordering INSIDE evidence: pull → report → destroy | ArtifactStep(EVIDENCE) ordering validated at plan construction |
| 2026-10-02 (templates) | same-run golden destroyed right after its own gate passed | frozen-keep respected by phase-1 waves | frozen_keep rides the ReclaimPlan |

Cross-cutting rule re-earned structurally: **the guarded path must be the only
ergonomic path** — the 2026-09-30 sweep was a bypass, not a missing guard. In
the new model there is no other path to write: any code that wants to destroy a
VM needs an Ownership object, and the only way to get one is the state file's
run id.

## 3. Object model

New package `teardown/`. `vm_ownership.py` stays top-level (preflight and the
deploy pipeline import it; moving it would invert the dependency) and gains
`Ownership`, the type that replaces loose `expect_tags` sets at every call site.

```
teardown/
  __init__.py        # public surface: TeardownEngine, TeardownPlan, Ownership re-export
  refusal.py         # Refusal(saw, expected, remedy) — the ONE refusal vocabulary
  inventory.py       # Inventory: one cached, classified view of the estate
  steps.py           # Step base + failure policies + the concrete steps
  plan.py            # TeardownPlan: ordered steps, evidence-ordering validated at build
  engine.py          # TeardownEngine: gates → journal → steps → verify_clean()
  modes.py           # TERMINAL_TEARDOWN vs RECLAIM plan kinds and their step lists
```

### 3.1 Refusal (`refusal.py`)

```python
@dataclass(frozen=True)
class Refusal(Exception):
    saw: str        # what was observed (vmid + tags, state field, endpoint, ...)
    expected: str   # what the ownership set / gate demanded
    remedy: str     # the one thing that would change this verdict
```

Rendered identically everywhere (`saw → expected → remedy`), raiseable, and
catchable as one type at every layer — including bad-auto, whose CLI prints a
machine-parseable line (§6). Every existing refusal (UNTAGGED, MISSING, no
run id, endpoint mismatch, frozen-without-intent, missing tfstate) migrates to
it with its current prose as the template — nothing loses information.

### 3.2 Ownership (`vm_ownership.py`)

```python
@dataclass(frozen=True)
class Ownership:
    comp: str
    run_id: str
    extra: frozenset = frozenset()   # GOLDEN_TAG, engine-template, jump tags

    @property
    def tags(self) -> frozenset      # the full expected tag set
    def verdict(self, vm_tags, *, marker_lookup) -> str   # owned|marker|unproven|missing
```

`ownership_verdict`'s four-verdict semantics are preserved byte-for-byte (the
characterization tests pin the refusal text, so no behavior drift is possible).
`expect_tags=` parameters across the codebase become `ownership=` parameters;
plain-set callers migrate mechanically via `Ownership.tags`.

### 3.3 Inventory (`inventory.py`)

```python
class Inventory:
    def __init__(self, nodes, api=proxmox_api)   # nodes: every hosting node
    def vms(self, node) -> list[Vm]              # cached listing
    def config(self, node, vmid) -> dict         # cached config
    def owned(self, ownership) -> list[Vm]       # full-set matches, all nodes
    def bridges(self, node) -> list[str]
    def bridge_in_use(self, node, bridge) -> bool  # fail-closed (unreadable = in use)
```

One snapshot per engine run (refreshed explicitly where a step must see fresh
state). Pre-stop, sweep, template teardown, verify_clean, and phase-1 all ask
the SAME Inventory — the classification can never disagree between callers
because there is only one caller shape. Fail-closed reads (a node that will not
list counts as "unknown, do not claim clean") are the Inventory's contract, so
verify_clean cannot read an API outage as success.

### 3.4 Steps (`steps.py`)

```python
@dataclass(frozen=True)
class FailurePolicy: kind: str   # REFUSE | WARN_AND_PROCEED

class Step:
    name: str
    kind: str                    # EVIDENCE | TEARDOWN
    failure: FailurePolicy
    ownership: Ownership | None  # blast radius; None = bookkeeping (no VM touches)
    def run(self, ctx) -> StepResult
```

Concrete steps: `PortalStep`, `ArtifactStep` (EVIDENCE), `RedStep`,
`PreStopStep`, `TerraformStep`, `TemplatesStep`, `RemoteAccessStep`,
`VerifyStep` — and the RECLAIM-only `SlotWavesStep`, `BridgeStep`. Each concrete
step wraps one existing ops function (the mechanics of `destroy_sweep_ops` are
largely correct and move mostly verbatim); what changes is that the failure
policy is DECLARED on the step and applied by the executor in one place, so the
warn-and-proceed try/except forest stops being copy-paste.

### 3.5 Plan (`plan.py`)

```python
class TeardownPlan:
    kind: str                    # TERMINAL_TEARDOWN | RECLAIM
    steps: list[Step]
    def validate(self): ...
    # TERMINAL_TEARDOWN: every TEARDOWN step must follow every EVIDENCE step.
    # RECLAIM (phase 1): evidence steps are exempt by design — the prior run's
    # evidence was that run's teardown's job; phase 1 only reclaims resources.
```

Construction-time validation turns the ordering invariant into a type check:
a plan with a TEARDOWN step before an EVIDENCE step cannot be BUILT, so no
future mode can reintroduce the 2026-10-09 race.

### 3.6 Engine (`engine.py`)

```python
class TeardownEngine:
    def __init__(self, comp_dir, env, inventory, *, journal_path=None)
    def execute(self, plan) -> TeardownResult
    def verify_clean(self, plan) -> CleanReport
```

`execute`: (1) run the gates (run-id anchor, endpoint guard, tfstate presence,
frozen-vs-intent — all refusing with `Refusal`); (2) load the journal
(`competitions/<id>/.teardown-journal.json`, atomic writes) and skip steps it
records as done — this is what makes re-running idempotent and replaces
`--skip-artifacts`; (3) run steps, honoring each step's failure policy, journal
each completion; (4) `verify_clean`: a FRESH Inventory confirms nothing owned
remains running (TERMINAL_TEARDOWN: nothing owned remains, period — kept
templates are reported as kept-by-design), red01's vmid is gone, no terraform
state lock, evidence archived. **Exit 0 iff the CleanReport is clean.**

## 4. Guarantee → type mapping

| Invariant | Today | Becomes |
|---|---|---|
| Ownership total & verified | tag-set comparisons at 6 call sites + prose refusals | `Ownership` — the only destroy-shaped API takes one; `Refusal` on unproven |
| Force by default, owned only | `status/stop` callsites + comments | PreStopStep/delete primitive: hard-stop is the only stop code path in `teardown/` |
| Evidence before destruction | comment-enforced ordering in 2 sequencings | `TeardownPlan.validate()` — cannot build an illegal plan |
| Resumable, idempotent | "re-run until clean" convention | journal + step idempotence; no flag gates resume |
| Concurrent-safe | full-tag-set checks, warned flock | same checks via `Inventory.owned(Ownership)`; flock warning unchanged |
| One refusal vocabulary | 3 shapes of prose | `Refusal(saw, expected, remedy)` raised/rendered everywhere |
| Exit 0 = clean | implied | `verify_clean()` — the only exit-0 path |

## 5. What gets deleted (and the flag ledger)

Deleted modules (same commit that stops calling them): `destroy_gate_ops.py`,
`destroy_sweep_ops.py`, `destroy_templates_ops.py` — their mechanics move into
`teardown/` steps; `destroy-competition.py` becomes a thin CLI shim (argparse →
plan → engine). `scrim/teardown_stage.py` collapses to shared steps + an
in-process engine call. Phase-1 (`deploy_lib/phases/cleanup.py`) becomes a
RECLAIM plan on the same engine; the pure wave computation
(`deploy_lib/golden_plan.phase1_destroy_waves`) stays.

Flags: `--skip-artifacts` DIES (journal makes it meaningless — collection is
skipped on resume exactly when it already succeeded). `--full` stays (named
operation: also destroy templates), `--end-of-competition` stays (frozen
intent), `--keep-red` stays, `--artifacts-timeout` stays, `--competition`/`--yes`
stay. DEFAULT MODE FLIPS to full-teardown for non-frozen comps (owner DoD:
"exit 0, no flags beyond --competition/--yes, nothing left tagged");
`--teams-only` is the explicit opt-in for the mid-run keep-templates mode, and
a frozen comp keeps requiring `--end-of-competition` for its templates.

Escape hatches: none return. A legacy range without a run id is still refused
(remedy: remove by hand on the node, per the run-id design note).

## 6. The bad-auto boundary

bad-auto's internals are NOT merged or redesigned away — it is a sibling repo
with its own tests. The boundary becomes a CONTRACT:

1. **Shared refusal line.** `badauto destroy` prints, on any identity refusal,
   one line: `TEARDOWN-REFUSAL {"saw": ..., "expected": ..., "remedy": ...}`
   and exits with a distinct code (17). The RedStep parses it into a `Refusal`.
2. **Evidence-before-destroy inside bad-auto.** Standalone `badauto destroy`
   currently deletes world.json/events.jsonl with the VM (the tezca wrapper's
   `/tmp/ba` staging is the only net, only on that path). badauto gains the
   stage+pull before its VM delete — the same guarantee at both call surfaces.
3. **Identity cross-check unchanged** (config.yaml / deploy stamp / VM marker);
   the redesign touches only its OUTPUT shape (the refusal line) and adds the
   evidence step before its delete.

## 7. Validation plan (live, from the worktree)

- **V1 clean cycle**: 1-team 2-box comp with assume_breach → deploy → destroy
  `--competition <id> --yes` ONLY → exit 0, zero WARNINGs, evidence archived,
  red01 gone, nothing owned left on the node.
- **V2 kill/resume**: re-deploy, SIGKILL the destroy mid-terraform (by pid),
  re-run → journal skips done steps, exit 0.
- **V3 foreign decoys**: an untagged VM at a computed slot and a
  same-comp-different-run-tagged VM inside the blast radius → both refused and
  reported, decoys survive the teardown, teardown still exits clean, decoys
  removed afterward.
- Transcript (commands + exits) appended to this file. Practice-run rule
  honored: new worktree, own scoring-vmid/identifiers/mgmt-IP block, preflight
  clean against the shared estate.

## 8. Status — SHIPPED (2026-10-12), live-validated

- [x] Characterization tests (19) pinning §4 — passing before AND after the redesign
- [x] This RFC (v1 design -> as-shipped below)
- [x] `teardown/` package + unit tests (`tests/test_teardown_model.py`)
- [x] Entry-point migration + old-module deletion (destroy_gate/sweep/templates_ops deleted)
- [x] Phase-1 RECLAIM migration (`deploy_lib/phases/cleanup.py` composes CallSteps)
- [x] Scrim stage migration (in-process via `teardown/runner.py`; harness evidence journalled)
- [x] bad-auto contract commits (TeardownRefusal + TEARDOWN-REFUSAL line + exit 17 +
      evidence-before-destroy; tezcatlipoca parses the contract)
- [x] Docs drain (AGENTS.md, usage-agents.md, internals.md, architecture.md, multi-node.md,
      usage-people.md, environment-facts.md; `test-run-loop.md` untouched — its reset command
      works verbatim)
- [x] Live validation V1–V3 — transcript in §9
- [x] Suite 1430 green (49 teardown-model/characterization tests; hermetic, 50s full suite)

Net: deleted `destroy_gate_ops.py` + `destroy_sweep_ops.py` +
`destroy_templates_ops.py` (357 lines) and the flag `--skip-artifacts`;
`destroy-competition.py` 269 -> 200 lines (presentation only); new `teardown/`
package ~1,100 lines incl. 49 tests. Four live-found defects fixed during
validation (§9): failed assume-breach attempts now recorded in state; RedStep
records the vmid from badauto's own target line; a fresh deploy clears the
previous teardown's journal; `destroy_golden_set` returns its skip list.

## 9. Live-validation transcript (worktree `../tezcatlipoca-teardown-redesign`, node .150, comp `tdred-2026`, engine vmid 2300, identifier 240, mgmt IP 10.0.0.253 — shared with a live concurrent deploy of scrim-one by the parallel test-loop session; distinct blocks, preflight warned-and-proceeded)

**V1 — clean deploy -> destroy with ONLY `--competition --yes`:**
```
python3 create-competition.py --competition tdred-2026 --teams 1 --yes ...   # exit 0 (after fixing comp sizing)
python3 destroy-competition.py --competition tdred-2026 --yes
  [portal] ok / [artifacts] ok (archived to ~/.tezcatlipoca/automated-tests/tdred-2026/run-0ec07c9e)
  [red01] warned — badauto destroy refused the mismatched target (correct: it
           must never touch the parallel session's red01)
  Skipping pre-stop of team1-web01 (vmid 1402) — tags 'comp-scrim-one;run-b6c2db3e;...'
           miss the expected ownership set; likely ANOTHER run       <- coexistence guard, live
  Pre-stopped team1-web01 (vmid 2600) / [terraform] ok (6 destroyed)
  [templates] ok / [remote_access] ok
  estate clean for this ownership set.                                      <- exit 0
python3 destroy-competition.py --competition tdred-2026 --yes; echo $?       # re-run: EXIT=0
```
Post-estate: zero `comp-tdred-2026` VMs; all 15 parked scrim-one VMs untouched.

**V3 — foreign VMs refused and reported:**
```
# decoy-untagged (vmid 2451, no tags) + decoy-foreigntag (2452, comp-tdred;run-12345678)
phase-1 reclaim: "phase-1 reclaim step 'waves:engine-node' failed: refused:
  refusing to destroy UNTAGGED vmid 2451 (decoy-untagged)"               <- fail-closed, live
teardown: decoy-foreigntag reported "same competition, DIFFERENT run — not touched",
  both decoys SURVIVED the destroy; removed by hand afterwards (the documented remedy)
badauto destroy --dry-run --competition <tdred> (contract worktree):
  "identity: comp=tdred-2026 id=... verified" -> guards pass             <- 3-way identity, live
badauto destroy --competition <tdred> --yes (contract worktree): red01 destroyed,
  "WARNING: red01 staged no evidence" honestly recorded, exit 0          <- evidence contract, live
```

**Live-found defects fixed during validation** (each with a regression test):
1. A FAILED assume-breach deploy left red01 standing while teardown exited 0 —
   failed attempts now record `{ok: False}` in state so verify_clean demands red01.
2. RedStep resolved red01's vmid from the SHARED bad-auto config.yaml (another
   session's deploy rewrites it) — now parsed from badauto's own verified target line.
3. The teardown journal survived a redeploy (same run id) and skipped red01 on the
   new range — phase 1 clears it on a fresh deploy.
4. `destroy_golden_set` swallowed its skip list — now returned and recorded.

**V2 — mid-teardown kill -> resume -> exit 0** (deploy #7; the kill is
SIGKILL to the whole process group, journal-triggered — the journal is
written atomically per step, so it is the filesystem-truth trigger; the
log line is block-buffered and the terraform lock window is ~15 s, both
unreliable triggers, found the hard way across deploys #3-#6):
```
kill -- -<destroy pgid> at t=14s: journalled done = [portal, artifacts, red01];
  pre_stop/terraform/templates/remote_access never ran; range still standing
# resume 1 (re-run of the same zero-flag command):
  [portal] ok / [artifacts] skipped — journalled done / [red01] skipped — journalled done
  [pre_stop] ok / [terraform] failed after 4 bounded attempts
  exit 1: NOT CLEAN — 'terraform' failed; state lock still present; "re-run (it is
          safe to re-run: teardown is idempotent)"          <- the honest exit contract
  (the stale-lock recovery had refused to clear the lock while ANY terraform was
   alive — the parallel session's deploy held `pgrep terraform` true for all 4
   attempts; fail-closed is correct, the wait is the price of sharing the host)
# resume 2 (no terraform alive any more):
  [pre_stop] ok / [terraform] ok / [templates] ok / [remote_access] ok
  estate clean for this ownership set.                      <- EXIT=0
```
Also live-proven en passant: the completion journal skips exactly the steps a
prior pass finished (artifacts/red01), on the SAME zero-flag command, with no
resume flag — invariant #4 as a first-class property.

