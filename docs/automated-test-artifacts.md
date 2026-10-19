# Automated test artifacts — one folder per run under the competition

Every automated test run (a scrim, a soak, a canary, a plain integration deploy) leaves a standard
folder of reports and evidence under the competition it tested, and teardown collects it **before**
it destroys anything — the red report and the blue logs live on machines that are about to be gone.
Implemented in the [artifacts_lib/](../artifacts_lib/) package behind the thin
[artifacts_lib/artifacts_ops.py](../artifacts_lib/artifacts_ops.py) facade (the stable API: callers
and tests patch names there); module map: [internals.md](internals.md#artifacts_lib).

## Layout and key

`competitions/<comp>/.automated-tests/<key>/` holds `test.json` (identity, intent, phases, verdict,
writeup status), `collection.json` (what was pulled, from where, sha256, per-target status),
`RED-TEAM.md`, `BLUE-TEAM.md`, `REPORT.md` and `evidence/` per the manifest in
`artifacts_lib/plan.py`; `index.json` rolls up every test in the competition. Red's files are
root-owned on red01, so the collector stages them through `/tmp/ba` before scping the copies.

**The key is the run id, and that is the point.** `run-<8hex>` is minted once per competition
directory, persisted in the state document, stamped on every VM as an ownership tag, and required
by every destruction path — so the artifact folder and the VMs it describes share one identity, and
two worktrees running the same competition ID cannot collide. A state document with no run id gets
`untagged-<deploy timestamp>` derived from the state document's mtime
(`artifacts_lib/paths.py::test_key`), so re-running teardown does **not** create a second folder.

The whole tree is **gitignored, with no publish path** (guarded by
`test_no_test_artifact_is_tracked`). These files quote flags, credentials and inject answers by
construction, and a password quoted in prose is exactly what the pattern net in
`tests/hygiene/test_secret_hygiene.py` cannot catch. A recommendation leaves this folder by being
written into [known-issues.md](known-issues.md) or a fixes plan, never by adding the artifact to
git.

## Who writes what

- `run-agent-scrim.py` creates the folder at run start and collects + generates the verdict
  **before** the red step destroys red01.
- `destroy-competition.py` is the safety net and the one step guaranteed to happen: it captures the
  engine's own final scoreboard when the harness never did, and collects right after the
  confirmation prompt, before its first destructive call — this is what saves a run whose harness
  died. Its `--team` mode is the exception: a mid-event team removal has no artifact step (no
  declared artifact lives on a team box); the removal is recorded in the state document's
  `deleted_teams` and in `teams.json`.
- `scrim-report.py` writes `INTERACTION.md`; its verdict folds into `test.json` verbatim, nothing
  here recomputes it.
- A human or agent writes `REPORT.md`'s judgement sections.

Collection **warns and proceeds** — a dead box never blocks a destroy, and there is deliberately
no gate: an operator who needs to destroy a range must never be held hostage by a wedged guest. A
`failed` transfer is retried once before the destroy makes a second attempt impossible;
`unreachable`/`unrecoverable` sources are not retried; what is still lost is printed as a
`verify_clean` note at the end of the destroy.

## Status vocabulary

`collection.json` records one status per wanted item from the closed set in
`artifacts_lib/constants.py` (`STATUSES`; the warning-producing subset is `LOST`). The point of the
vocabulary is that "no report" and "the pull failed" are different facts: `absent` is a claim about
a box (the source answered, the file genuinely is not there), `failed` means the transfer broke,
`unreachable`/`unrecoverable` are about the source. What could not be obtained is recorded in
`collection.json`, restated in the affected document as a stub, and listed in the report's *Not
verified / caveats*. `not-collected` appears in no collection record — it is a canonical document's
stub status meaning the sources exist but nobody ran the collector (recoverable; the stub names the
command).

## Reading a test

```bash
python3 test-artifacts.py list <comp>                  # every test in the competition
python3 test-artifacts.py show <comp> <key>            # identity, collection, problems
python3 test-artifacts.py plan <comp> <key>            # what collection would attempt, and why
python3 test-artifacts.py verify <comp> <key>          # re-hash everything; --seal when done
python3 test-artifacts.py collect <comp> <key>         # retry a failed pull by hand
python3 test-artifacts.py archive <comp> --all         # copy outside the repo (worktrees)
```

`REPORT.md` is the document a person reads: machine verdict first (never a second opinion of the
same numbers), then environment, timeline, cost, a seeded incident table, red and blue narratives,
and two fixed recommendation headings — one for the competition, one for tezcatlipoca. Every
machine section is filled by teardown; judgement sections are marked `<!-- TODO(author) -->` and
`test.json.writeup.status` stays `needs-writeup` until `test-artifacts.py verify --seal` confirms
them filled and re-hashes the collection. A second teardown never overwrites a report that has
been written.

## Durability

The competition dir is per-worktree, and practice runs must happen in a throwaway worktree
([AGENTS.md](../AGENTS.md)), so the folder is copied outside the repo —
`${TEZ_ARTIFACTS_ARCHIVE:-~/.tezcatlipoca/automated-tests}/<comp>/<key>/` — automatically when
teardown detects a linked worktree, and again when the write-up is sealed so the archive is not
left holding a skeleton. **Archive before `git worktree remove`.**

## What it does not do

- **No engine submission bodies.** The engine capture is a copy of what the harness dumped before
  the scoring DB died: injects, per-team services and the final scoreboard. A submitted *file* is
  captured only if the blue agent wrote it into its workdir.
- **No bare-deploy stdout.** A bare `create-competition.py` leaves console scrollback only; a run
  driven that way has its phase timeline in `.deploy-timings.jsonl`, the harness phase markers, and
  the operator's own `tee` ([e2e-testing.md](e2e-testing.md)).
- **No opinion about the run.** Success is the interaction score and the gates in
  [rehearsal-gates.md](rehearsal-gates.md); the artifact folder reports them, it does not redefine
  them.
