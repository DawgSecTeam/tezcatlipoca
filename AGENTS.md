# AGENTS.md — tezcatlipoca

`tezcatlipoca` is the Proxmox-based scoring-range driver: `create-competition.py` generates
Quotient's scoring config and nakon's machine list, then runs an eight-phase deploy. Companion
scripts: `destroy-competition.py`, `redeploy-competition.py`, `verify-competition.py`.

All guidance lives in `docs/` — start with:

- [README.md](README.md) — quickstart + full docs index
- [docs/README.md](docs/README.md) — the grouped index of every doc
- [docs/architecture.md](docs/architecture.md) — architecture, nakon contract, operational invariants
- [docs/internals.md](docs/internals.md) — per-module design notes
- [docs/usage-agents.md](docs/usage-agents.md) — driving the pipeline non-interactively
- [docs/usage-people.md](docs/usage-people.md) — setup/operation by hand, config reference, troubleshooting
- [docs/multi-node.md](docs/multi-node.md) — multi-host competitions
- [docs/packet-profiles.md](docs/packet-profiles.md) — competition packets → ranges
- [docs/remote-access.md](docs/remote-access.md) — headscale participant access
- [docs/known-issues.md](docs/known-issues.md) — open/pending issues only; drained every run cycle
- [docs/environment-facts.md](docs/environment-facts.md) — host-agnostic template/vmid/runtime behavior
- [tlaloc/AGENTS.md](tlaloc/AGENTS.md) — the red agent's own rules; it lives in this tree at
  `tlaloc/`, and deploy phase 7 stages it onto red01 (`red/tlaloc_ops.py`)

## Estate ground truth is gitignored

Which hosts exist, how to reach them, their node names, datastores, API-token names and
sources, template vmids, live vmid ownership, and shared-service addresses live in
**`infrastructure-status.md`** (repo root, gitignored, local only). **Read it before any
deploy, destroy, or env edit.** That is the only place those facts belong: a tracked doc,
prompt, commit message, or test fixture may name the file, never the values. If a doc and
that file disagree, the file wins — fix the doc.

## Range runs go through the dashboard

The central run queue (tezcatlipoca-dashboard; address and token in `infrastructure-status.md`,
MCP at `/mcp`) is where
range runs — deploy/verify/destroy/scrim — are submitted and observed. It owns per-run engine
worktrees at pinned refs, pushes stage events to subscribed sessions (`watch_run` — wait for the
push, don't poll logs); concurrent runs — even of one competition — are allowed, with the
engine's preflight/locks refusing provable resource collisions. Competition
definitions live in `tezcatlipoca-competitions`; the drivers resolve them via `--comp-dir` or the
comps root (`TEZ_COMPETITIONS_ROOT`, else `./competitions`, else the sibling
`../tezcatlipoca-competitions`). Direct driver runs are the recovery path only.

## Practice runs must run from a new worktree

**Any practice run (capacity test, canary, shakedown — any deploy testing the pipeline rather than
hosting an event) MUST run from a NEW worktree cut off `main`**, never from the main tree or
another run's worktree. Practice deploys crash and leave half-built state; isolating them keeps
main's tree and node view clean and stops two sessions driving the same checkout. The dashboard's
per-run worktrees satisfy this mechanically; the rule binds manual runs.

Each deploy stamps a per-comp-dir `run-<id>` tag on everything it creates; every destroy path
requires the full ownership set, so two worktrees may deploy the SAME competition ID without
their teardowns eating each other's VMs. Coexistence needs distinct `--scoring-vmid` and team
identifiers — preflight refuses real collisions. A state file with no run id is refused by
teardown; untagged VMs are never destroyed unless they carry this competition's clone marker.
Details: [docs/usage-agents.md § run ownership](docs/usage-agents.md#run-ownership).

**Concurrent ranges are supported, from isolated worktrees.** Nothing warns when another deploy
is live against the same estate — the preflight refuses real collisions (vmid clashes, engine
mgmt-IP conflicts) wherever provable, and `acquire_engine_lock` refuses a second deploy against
the same scoring engine. Safe coexistence is otherwise on you: distinct `--scoring-vmid` and
`--team-identifiers` blocks per range, relying on run-id tags to scope teardown.

**red01 is tezcatlipoca's VM, and its identity is per-competition.** Its vmid is reserved per
comp (Compfile `red01_vmid`, else derived one golden stride past this placement's last band) and
its IP is selected from the red band `10.0.0.240-10.0.0.249` at deploy time (`red01_ip` overrides
it). The vmid is preflight-gated like the engine's — an occupied slot is a refusal naming the
owner, never a silent default. The VM carries the run's ownership tags, so two worktrees may
deploy ranges with red01s concurrently with zero coordination, and teardown destroys only what
its own tag set proves it deployed.

Teardown after a practice run is `destroy-competition.py` (`teardown/`,
[docs/usage-agents.md § destroy-competition.py](docs/usage-agents.md#destroy-competitionpy)). It
is resumable (a completion journal skips finished steps; retry a warned step by re-running), and
exit 0 means the estate is VERIFIED clean for this deploy's ownership set. **Never substitute
ad-hoc destroy scripts** — every destroy-shaped API takes the deploy's `Ownership` (comp tag +
run id) and refuses anything less; this is the only path that can destroy a VM. Every teardown
path force-stops the VMs it owns before deleting — never graceful ACPI shutdown, which gains a
doomed VM nothing and hangs on agent-less boxes (graceful shutdown is only for VMs that survive
the operation).

**Templates are the thing a practice run exists to validate, and they SHOULD be kept.** A full
teardown destroys them, so a practice range built to host an event is torn down with
`destroy-competition.py --teams-only` (or, for a scrim, `run-agent-scrim.py --keep-templates`) —
team boxes, engine, bridges and red01 go; the golden + engine templates stay, and the next deploy
of the SAME competition **reuses the exact artifacts that were tested instead of rebuilding them**.
That reuse keys on the `tezcatlipoca-m4 hash=` stamp on the template itself, not on the
per-comp-dir `estate-state.json` (a fresh worktree and every dashboard workspace lack it), so
it survives the move from the practice worktree to the event's. Config drift still rebuilds;
goldens never carry across competitions, so a full teardown is still how a competition's templates
are finally reclaimed once its run's goal is met. Details:
[docs/internals.md § template lifecycle](docs/internals.md#template-lifecycle-engine-template_freeze-py-engine_template_ops-py).

Teardown collects this run's reports and evidence into
`competitions/<id>/.automated-tests/<run-id>/` **before** it destroys anything (red report, blue
logs, engine capture, then `REPORT.md` with machine sections filled and judgement sections marked
`TODO(author)`). It warns and proceeds on a dead box; what it couldn't get is recorded in
`collection.json` and the report's caveats. There is no `--skip-artifacts`: a resume's journal
marks collection done and skips it. **Because the competition dir is per-worktree, archive before
discarding the worktree:** `python3 test-artifacts.py archive <comp> --all`, or keep the copy
teardown already made under `~/.tezcatlipoca/automated-tests/`.

A linked worktree has none of the gitignored local state the deploy reads, and every path resolves
from the worktree root — run all commands from there. Pre-flight:

```bash
git worktree add -b <branch> ../tezcatlipoca-<branch> main
cd ../tezcatlipoca-<branch>
git submodule update --init                          # vendor/nakon — every nakon call needs it
cp /path/to/main-tree/tezcatlipoca.toml .            # the NODE-PROPER profile (see below)
cp /path/to/main-tree/.env .env                      # TEZ_* env overrides, still loaded
cp /path/to/main-tree/vendor/nakon/.env vendor/nakon/.env
cp /path/to/main-tree/proxmox . && chmod 600 proxmox # the SSH key the config points at
```

- Pick the config matching the target node, and check it for stale values before trusting it:
  every banner prints the resolved endpoint/node/datastore, so a wrong copy shows immediately.
  Known traps to check for in any config variant: a dead `proxmox.template_vm_id` (hard-fails the
  engine-base preflight — correct per-node values are in `infrastructure-status.md`); a default
  `engine.mgmt_ip` that collides with a live engine on a shared node (set it explicitly —
  see `infrastructure-status.md`); and no `knobs.team_identifiers`
  on a shared node, where the default 101+ block is commonly already occupied — set it explicitly.
- All competition ops on Proxmox run on the range automation token, never the workshop portal's
  provisioner token (token names and per-host token sources: `infrastructure-status.md`). When
  copying a config across nodes,
  re-export the token as `TEZ_PROXMOX_API_TOKEN` explicitly
  instead of inheriting the source profile's value; check it before any deploy/destroy.
- `TEZ_THIN_HEADROOM=<0..1>` relaxes the datastore headroom gate on thin-provisioned pools (ZFS,
  lvmthin): it counts that fraction of the provisioned team-disk math, since linked clones only
  allocate written blocks. Unset keeps the strict provisioned-bytes gate. Size the factor to the
  pool, not to hope.
- Everything red needs is in this tree (`red/red01_ops.py`, `tlaloc/`): there is no sibling
  checkout to copy a key or a config into. The deploy's identity is the run's tag set in its own
  state document, not a file another session can rewrite.

Then the normal flow: `create-competition.py --competition <id> --scoring-vmid <free> --plan-only`,
real `--teams N --yes`, verify, destroy — all from the worktree root.

## Editing and dispatch discipline

- **Fix causes, not symptoms — this is the project's first discipline.** A test
  run exists to find defects so they can be killed at the root: the goal is a
  stack that runs end-to-end with no agent intervention, not a run that an
  agent shepherds past its failures. Diagnose from evidence before retrying; a
  failure you cannot explain is not resolved, and "flaky" means broken. No
  workarounds during a run (no
  manual SSH repairs, hand-edited VMs, hand-cleared flags, `--force`) — if a
  workaround feels necessary, the thing that made it feel necessary is the
  bug. Fixes may cross layers when that is the elegant fix; the exception is
  the blast-radius rules (teardown tag matching, run ownership, never sweep),
  which are safety rails, not workarounds. Architectural redesigns and
  anything that could touch another competition's infrastructure go to the
  user first; everything else is yours to decide, with the reasoning recorded.
- **Complexity belongs in the tool, not the operator.** Anything a guide or
  prompt tells an agent to "remember" or "watch out for" is a bug filed
  against the code. When you write a trap recipe, also file — and prefer to
  fix — the defect that made the recipe necessary.
- **Read a file before editing it**, and prefer a small anchored edit over a large literal block.
  `File has modified since read` is concurrency, not carelessness, when several sessions share a
  tree — expect it and re-read.
- **No mutating shell commands from plan mode.**
- **Never re-dispatch an identical subagent task** after it fails. A failed delegation is not a
  reason to send the same prompt again. Prompts should name the artifact the subagent must
  produce, so a retry can see what already exists.
- **Range runs go to the dashboard, long manual operations are detached** — see
  [usage-agents.md § central runs](docs/usage-agents.md#central-runs-the-dashboard). Never wrap a
  deploy in `timeout` to make it fit a tool call, and never carry an hour-scale run in a background
  tool task — submit it to the dashboard; for manual recovery use `setsid nohup` plus log polling.
- **Push after changes**: committing is not finishing — push to origin as part of the task. Fetch
  and check divergence first; rebase rather than force-push (force-push is for sanctioned history
  rewrites only, never casually). Never push anything carrying live secrets.
- **One commit per branch**: a finished branch lands on main as a SINGLE squash commit with a
  brief one-line message — `git merge --squash <branch>` + commit, never a merge commit carrying
  the branch's incremental train. Stale worktrees must NOT `git pull` against a rewritten main —
  finish and squash-merge, or re-cut the branch.
- **Before a long operation, check for a twin.** Parallel agent sessions are normal here, and two
  sessions driving the same competition/branch reap each other's deploys and live state. Check
  `pgrep -af create-competition` and recent `git log` for fresh co-authored commits — also the
  first move when file state "contradicts" what you saved, before assuming corruption.
- **Capability claims need the remote nakon catalog.** The pinned submodule shows mechanism only —
  the configs live in the remote vulndb — so "not supported" concluded from grepping the submodule
  is wrong by construction: run `python3 -m nakon catalog list --json` first.
- **Kill by pid or process group**, never `pkill -f` a pattern that also appears in the invoking
  command line.
- **Never judge a command's success through a pipe** — `cmd | tail` reports the PIPE's exit code,
  so a failed pytest/deploy reads as success. Check `${PIPESTATUS[0]}`, or redirect to a file and
  echo `$?` from the command itself.
- **Never write "validated" in a commit message before it is.** State what was done and what the
  next step proves; the report/docs are where validated claims live after they pass.

## Code style

**Minimum code (standing):** write the minimum code that solves the
problem — no error handling, input validation, logging, config options, or abstractions not asked
for; no try/except around code that can't meaningfully fail; no helpers used only once (flat over
nested, inline over extracted). Comments explain WHY only (non-obvious intent, workarounds,
gotchas), default zero, never narration; no docstrings except public API, then one line without
restating types. Change only what's needed — no reformatting, renaming, or "cleanup" of untouched
lines. After changes: no preamble, no summary — explain only decisions the user would likely
question. Design "why" prose belongs in `docs/internals.md`, not comments.

## Editing docs

Docs are held to the same standard as code: **verify every claim against the source before writing
it** (there is one pipeline — golden templates + linked clones — and passages written against
older pipelines may still turn up), prefer one canonical statement plus links
over copies. If a doc and the
code disagree and the code looks like the bug, document the code's actual behavior and flag the
discrepancy (`docs/known-issues.md`) rather than silently "fixing" the doc. Fixed issues are
deleted from `docs/known-issues.md`, not archived — git history
is the record. Never edit code from a
docs pass. Run/scrim reports lead with interaction reality: blue presence and red-blue interaction
counts first — a red-vs-empty-room run is a failed run no matter how good red's log looks, and is
never framed as a red success.
