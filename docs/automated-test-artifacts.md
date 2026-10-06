# Automated test artifacts — one folder per run under the competition

Every automated test run (a scrim, a soak, a canary, a plain integration deploy) leaves a standard
folder of reports and evidence under the competition it tested. Teardown collects it **before** it
destroys anything, because the red report and the blue logs live on machines that are about to be
gone.

Implemented in the [artifacts_lib/](../artifacts_lib/) package, imported through the thin
[artifacts_ops.py](../artifacts_ops.py) facade (the stable API: `ensure_test`, `collect`,
`collect_for_teardown`, ... — callers and tests patch names there); inspected and sealed with
[test-artifacts.py](../test-artifacts.py), whose subcommands live in `artifacts_lib/cli_*.py`.
Module map: [internals.md](internals.md#artifacts_lib--per-run-test-artifacts). Design record and the alternatives that were rejected:
[reports/automated-test-artifacts-plan-2026-10-03.md](reports/automated-test-artifacts-plan-2026-10-03.md).

## Layout

```
competitions/<comp>/.automated-tests/
  index.json                 roll-up of every test in this competition, newest first
  <key>/                     key = the deploy's run-<8hex>, else untagged-<deploy timestamp>
    test.json                identity, intent, phases, verdict, writeup status  (0600)
    collection.json          what was pulled, from where, sha256, per-target status
    RED-TEAM.md              pulled from red01, or an honest stub saying why not
    BLUE-TEAM.md             blue's authored report, sealed, or a stub
    REPORT.md                synthesis: incidents, run success, recommendations
    evidence/
      red/                   events.jsonl, world.json, report-<ts>.md, report-secrets.md, journal
      engine/                final scoreboard, injects, per-team services (captured at run end)
      blue/                  LOG.md, NOTEBOOK.md, feed.log, cycles/, sub*.md, submissions/
      harness/               run.json, T0.txt, monitors, INTERACTION.md, alerts, timings sidecar
```

**The key is the run id, and that is the point.** `run-<8hex>` is minted once per competition
directory ([utils.py](../utils.py)), persisted in `.deploy_state.json`, stamped on every VM as an
ownership tag ([constants.py](../constants.py)), reused across resume/redeploy, and required by every
destruction path. So the artifact folder and the VMs it describes are keyed on the same identity, and
two worktrees running the same competition ID cannot collide. State that predates run ids (all seven
comp dirs on disk as of 2026-10-03) gets `untagged-<deploy timestamp>` — derived from
`.deploy_state.json`'s mtime, so re-running teardown does **not** create a second folder.

The whole tree is **gitignored, with no publish path** (`.gitignore` +
`test_no_test_artifact_is_tracked`). These files quote flags, credentials and inject answers by
construction: the red report ships a separate 0600 credlist table, and a password quoted in prose is
exactly what `tests/test_secret_hygiene.py`'s pattern net cannot catch. A recommendation leaves this
folder by being written into [known-issues.md](known-issues.md) or a fixes plan, never by adding the
artifact to git.

## Who writes what

| Writer | What it contributes |
|---|---|
| `run-agent-scrim.py` | Creates the folder at run start (identity, teams, boxes, phases), records red/blue identity and workdirs when they are known, and collects + generates the verdict **before** `badauto destroy` erases red01. |
| `destroy-competition.py` | The safety net, and the one step guaranteed to happen: it collects right after the confirmation prompt and before its first destructive call. This is what saves a run whose harness died. |
| `scrim-report.py` | `INTERACTION.md` — the interaction score and the `rehearsal-gates.md` gate table. Its verdict is folded into `test.json` verbatim; nothing here recomputes it. |
| A human or agent | `REPORT.md`'s judgement: the incident classification and both recommendations sections. |

Collection **warns and proceeds** — a dead box never blocks a destroy. What could not be obtained is
recorded in `collection.json`, restated in the affected document as a stub, and listed in the
report's *Not verified / caveats*. There is deliberately no gate: an operator who needs to destroy a
range must never be held hostage by a wedged guest.

## The status vocabulary

`collection.json` records one status per wanted item, from a closed set. This is what makes the
folder honest — "no report" and "the pull failed" are different facts:

| status | meaning |
|---|---|
| `ok` | fetched |
| `sealed` | local file copied, hashed, 0600 |
| `absent` | the source was reachable and the file genuinely is not there |
| `failed` | the source was reachable and the transfer failed |
| `unreachable` | the source could not be reached at all (VM stopped, agent down) |
| `unrecoverable` | the source is gone for good (VM already destroyed) |
| `skipped` | not applicable to this run (no red agent in a plain deploy), or not attempted after the target was found unreachable |

`failed`, `unreachable` and `unrecoverable` are the ones that produce a warning. A target is only
probed for its first file once it proves unreachable, so a dead red01 costs one connection timeout
rather than five.

A canonical document that could not be produced is still written, as a stub naming its own status —
which adds one value to the vocabulary above that never appears in a collection record:
**`not-collected`**, meaning the sources exist but nobody ran the collector (recoverable; the stub
names the command). It is deliberately distinct from `absent`, which is a claim about a box.

## Reading a test

```bash
python3 test-artifacts.py list <comp>                  # every test in the competition
python3 test-artifacts.py show <comp> <key>            # identity, collection, problems
python3 test-artifacts.py plan <comp> <key>            # what collection would attempt, and why
python3 test-artifacts.py verify <comp> <key>          # re-hash everything; --seal when done
python3 test-artifacts.py collect <comp> <key>         # retry a failed pull by hand
python3 test-artifacts.py archive <comp> --all         # copy outside the repo (worktrees)
```

`REPORT.md` is the document a person reads. It opens with the machine verdict (never a second
opinion of the same numbers), then environment, timeline and cost, a seeded incident table, red and
blue narratives, and two fixed recommendation headings — one for the competition, one for
tezcatlipoca. Every machine section is filled by teardown; judgement sections are marked
`<!-- TODO(author) -->` and `test.json.writeup.status` stays `needs-writeup` until
`test-artifacts.py verify --seal` confirms they are filled and re-hashes the collection. A second
teardown never overwrites a report that has been written.

## Durability

The competition dir is per-worktree, and practice runs must happen in a throwaway worktree
([AGENTS.md](../AGENTS.md)), so the folder is copied outside the repo —
`${TEZ_ARTIFACTS_ARCHIVE:-~/.tezcatlipoca/automated-tests}/<comp>/<key>/` — automatically when
teardown detects a linked worktree, and again when the write-up is sealed so the archive is not left
holding a skeleton. Archive before `git worktree remove`.

## What it does not do

- **No engine submission bodies.** The engine capture is a copy of what the harness dumped before
  the scoring DB died: `/api/injects` (each inject's `Submissions` array — `InjectID`,
  `SubmissionFileName`, `SubmissionTime`, `Team`, `TeamID`, `Version`), per-team services and the
  final scoreboard, under `evidence/engine/`. The submitted *file* itself is not in that dump; it is
  captured only if the blue agent wrote it into its workdir (`sub*.md` → `evidence/blue/`).
- **No deploy stdout by default.** A deploy log is written into the run dir only when the deploy
  fails, so a successful run's phase timeline comes from `.deploy-timings.jsonl`, the harness phase
  markers, and the operator's own `tee` if they made one. Capturing stdout always is a separate,
  still-open upgrade ([e2e-testing.md](e2e-testing.md)).
- **No opinion about the run.** Success is the interaction score and the gates in
  [rehearsal-gates.md](rehearsal-gates.md); the artifact folder reports them, it does not redefine
  them.
