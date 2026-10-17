# AGENTS.md — working in this repo

Read this before changing anything. It is the authoritative map of the
architecture, the invariants that are not negotiable, and the way this repo is
verified. When this file and the code disagree, the code wins and this file is
the bug.

## What this repo is

tlaloc v2 — the red-team component of the tezcatlipoca scoring range. An
assume-breach CCDC red teamer: it is already inside when the event starts, then
pressures scored services while adapting to blue. It is a **rebuild** of v1
(GitHub `DawgSecTeam/tlaloc`, the `bad-auto` checkout beside this tree); the v1
audit's findings are what every module here is shaped by. This folder is part of
the tezcatlipoca tree: the range deploys it through `red/tlaloc_ops.py`, and the
ownership split (the engine owns the range, tlaloc owns decisions and effects) is
in [`docs/deploy.md`](docs/deploy.md). Read the docs before proposing an
architecture change.

## Non-negotiable invariants

1. **One action path.** Every action — the model's pick, an escalation, the
   pressure budget, a seeded plant, a C2 task — goes through
   `effects.execute()`. That template runs, in this order: kill switch →
   param shape → `ensure_target` → free-form lint (per target OS) → the effect's
   `check()` → dry-run short-circuit → `run()` → ledger/artifact/restore
   bookkeeping → audit. An effect must NOT re-implement any of that.
2. **`GuardrailError` halts, `PolicyError` rejects one candidate.** A guardrail
   breach means something tried to touch what it must never touch (out-of-ROE
   target, kill switch); a policy breach means this candidate is not legal now
   (ladder tier, cap, window) and the loop re-plans. Never swallow a guardrail
   breach into a `Result(False, ...)`.
3. **`ok=True` means the objective was met.** Every give-up path returns
   `ok=False` with a detail that says so. A C2 implant that installed but never
   checked in is `ok=False` **and** `installed=True` — the record is written from
   the install, never gated on a channel proof (v1 wrote nothing when a check-in
   was slow, and both blue's and the organizer's artifact lists went blind).
4. **Attribution is code-side.** An effect that takes a scored service down sets
   `Result.impact`; the ledger never depends on a caller remembering a
   `service=` argument. Conversely, a takedown whose damage did not hold is NOT
   an impact.
5. **The model never opens a gate.** clef's answers choose between candidates;
   `command_intent` informs attribution only; hard safety stays deterministic.
6. **The run must survive a dead model.** clef down → deterministic candidate #1;
   LLM down → everything but command authoring still works; both down → the
   deterministic ladder. Log the degradation with its reason.
7. **Artifact names come from the pool.** Every planted path/unit/account/task is
   derived from `effects/artifactnames.py` (drawn per plant, skipping names the
   event already wore). Never introduce a literal like `tznet-updates` — a fixed
   name is a free IOC for blue for the rest of the event.
8. **Facts come from intel.** Boxes, credentials and planted vulns come from
   `intel/` (nakon artifacts, with a blind fallback). `Intel.boxes()` is the
   single choke point and it skips `unmanaged` boxes — an in-path firewall is
   infrastructure, never a target.
9. **Every recoverable mutation carries its undo.** `Result.restore` with the
   exact recipe; mutations are MOVE/COPY, never `rm` and never truncate-in-place.
   Secrets only ever go into the organizer-facing 0600 artifact.
10. **The digest is sized to the model's batch.** `Decider.compose()` builds the
    question bundle first and gives the digest the remainder of
    `clef.max_request_tokens` (16384 since 2026-10-16; a fixed-size digest with a
    growing bundle 500s the endpoint mid-event). At that ceiling the whole state
    and the full option list go out; `policy.frame()`'s truncation cascade may
    drop `recent`, per-box `resp`, boxes past 2/team and untouched boxes — never
    ROE/phase/caps — and it is the fallback for a smaller endpoint, not the
    normal path.
11. **Every config key an effect reads is in `cli.CONFIG_CONTRACT`.** The contract
    is the per-field list the deploy pushes to `/etc/tlaloc/config.yaml` and
    `verify-config --pushed` diffs; a key read by the agent but absent from it is
    silently dropped on the way to the VM (v1 shipped a whole feature that way).
    Add the key to `config.DEFAULTS`, the contract, and
    `tests/test_config_contract.py`'s table together — that test fails otherwise.

## The loop, in order (run.py `Loop.step`)

kill switch → world reload → sensors (scoreboard + `diff_restores` → blue events)
→ C2 callback mirror → artifact health (dead artifacts = the re-plant trigger)
→ `policy.candidates()` → `decider.next_effect()` (composes the digest, asks clef,
gates on `theta`, arbitrates if high-stakes and unconfident, else deterministic)
→ `escalate_now` (re-derive the mode of an already-legal impact candidate) →
`pressure_count` (extra bounded takedowns) → per-candidate `policy.validate()`
→ `execute()`. Circuit breakers: consecutive cycle errors (`error_halt_after`),
an unreachable range (`unreachable_halt_after`). `host_guard` refuses the live
loop off red01.

## Effects

Adding an effect: one module in `tlaloc/effects/`, one `@register`ed class, add
the module name to `effects.MODULES`, and add a dry-run sample to
`tests/test_effects.py::test_every_registered_effect_survives_dry_run` (the test
fails until you do). The full contract is in `docs/effects-authoring.md`; the
reference implementations are `recon.py` (read-only) and `takedown.py` (pressure
with mechanisms, both OSes, proof-of-damage, restore recipes).

The catalogue is 12 effects. Optional flavour effects from v1 (`decoy`,
`mysql_udf`) are deliberately NOT in the catalogue — they come back behind config
flags only if the club wants them, because each one is surface without pressure.

## State

`World` (`state/world.py`) is the agent's only belief store: one schema-versioned,
comp-stamped, atomically-written, flock'ed JSON document. Loader rules: a state
file for another comp or another schema is REFUSED (the fresh-state-dir-per-comp
contract is enforced, not remembered). Concurrency: reload-then-mutate-then-save,
no merge, disk is truth. `world.ensure_event_start()` saves only on create.

## Testing and verification

- **Runner: `python3 -m pytest tests/ -q`.** `unittest discover` silently skips
  pytest-only files — that is how v1 reported green while a real regression test
  never ran. CI is pytest.
- **Hermetic by default:** `tests/conftest.py` patches `subprocess.Popen`,
  `socket.create_connection` and `socket.socket.connect` to raise, so a test that
  dials a range address fails loudly instead of "passing" against the club LAN
  (v1's destroy tests did exactly that). Opt out per-test with
  `@pytest.mark.net`.
- **Fakes, not mocks of the code under test:** `tests/helpers.py` gives
  `make_ctx()` (temp state dir, real World/Gate/EventLog, `FakeSSH` scripted
  transport, `FakeC2`, `FakeSensors`), `base_cfg()`, `make_intel()`. No
  `object.__new__` constructions anywhere.
- **Replay:** `tests/fixtures/decisions-recorded.jsonl` holds real decision
  bundles + answers + probabilities; `tests/test_decide.py` replays them through
  `decide.py` with a stubbed clef and asserts the chain (threshold → act /
  arbiter / deterministic). A decision-layer change that alters recorded
  behaviour fails there, offline, with no model and no range.
- **The one live-ish dependency** is nmap presence; tests that exercise `recon`
  monkeypatch `shutil.which`.
- `python3 -m tlaloc --dry-run run --cycles 1` is the local smoke: it walks the
  whole cycle without touching a box (dry-run short-circuits `run()` but NOT
  `check()`, deliberately — an operator should see which candidates the ROE
  rejects).

## Conventions worth knowing before you "fix" something

- Times in reports and status output are **EST/EDT**, never UTC.
- `run.state_dir` is per competition. Never reuse a state dir across events and
  never "reset" one in place (v1's `--reset-event` leaked tallies, seed state and
  burndown rows into the next event).
- Config: `DEFAULTS < config.yaml < env allow-map`; a missing explicit
  `--config` path is a hard error (v1 silently ran on defaults once, and
  deployed the wrong VM). Config is frozen once the context is built — anything
  that changes mid-run lives in world state or the Compfile.
- Anything a deploy pipeline pushes to red01 must be in
  `cli.CONFIG_CONTRACT`; `tlaloc verify-config` diffs the effective config
  against a pushed one per field. A field the allowlist omits is a feature that
  is dead on the VM while every local test stays green.
- The engine owns red01, the red segment, the C2 DNAT (30s re-assert) and
  destroy. tlaloc supplies the restore table; it must never depend on being the
  thing that tears the range down.
