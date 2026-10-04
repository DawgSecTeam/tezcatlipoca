# AGENTS.md — tezcatlipoca

`tezcatlipoca` is the Proxmox-based scoring-range driver: `create-competition.py` generates
Quotient's scoring config and nakon's machine list, then runs an eight-phase deploy. Companion
scripts: `destroy-competition.py`, `redeploy-competition.py`, `verify-competition.py`.

All guidance lives in `docs/` — start with:

- [README.md](README.md) — quickstart + full docs index
- [docs/README.md](docs/README.md) — the grouped index of every doc ("read this when…")
- [docs/architecture.md](docs/architecture.md) — architecture, nakon contract, operational invariants
- [docs/internals.md](docs/internals.md) — per-module design notes (the "why" behind the code)
- [docs/usage-agents.md](docs/usage-agents.md) — driving the pipeline non-interactively
- [docs/usage-people.md](docs/usage-people.md) — setup/operation by hand, env-var reference, troubleshooting
- [docs/multi-node.md](docs/multi-node.md) — one competition across multiple Proxmox hosts (nodes.json placement, jump routing, per-slot goldens, sync-template)
- [docs/packet-profiles.md](docs/packet-profiles.md) — competition packets → ranges (profiles, compile-packet, packet verify gates)
- [docs/known-issues.md](docs/known-issues.md) — **open/pending issues only; fixed issues are deleted, not archived**
- [docs/environment-facts.md](docs/environment-facts.md) — node/storage/template/vmid ground truth
- [docs/security-disclosures.md](docs/security-disclosures.md) — credential exposures + the open rotation
- [docs/upstream-defects-handoff.md](docs/upstream-defects-handoff.md) — catalog/nakon defects to hand off

## Practice runs must run from a new worktree

**RULE — any practice run (capacity test, canary, shakedown, any deploy whose goal is testing the
pipeline rather than hosting an event) MUST run from a NEW worktree cut off `main`**, never from the
main tree and never from another run's worktree. Practice deploys are exactly the runs that crash,
get killed, and leave half-built state behind; isolating them keeps main's tree, state, and node view
clean, gives the run its own branch to discard or merge, and stops two sessions from driving the same
checkout.

Run identity makes this safe on the host too (2026-10-02): each deploy stamps a per-comp-dir
`run-<id>` tag on everything it creates and every destroy path requires the full ownership set, so
two worktrees may deploy the SAME competition ID without their teardowns eating each other's VMs —
coexistence still needs distinct `--scoring-vmid` + team identifiers (preflight refuses real
collisions). Legacy state without a run id: teardown's sweep stays OFF until `--legacy-tags`, and
untagged VMs need `--allow-untagged`. Details:
[docs/usage-agents.md](docs/usage-agents.md#run-ownership-teardown-only-touches-this-deploys-vms-2026-10-02).

**The preflight now enforces the other half of this.** Before anything else it refuses to start when
another deploy is live against the same estate — detected by a *held* flock in
`~/.tezcatlipoca/locks/`, which is the one signal that cannot lie (a stale `.lock` from a dead run is
ignored, not treated as a competitor). Two sessions at once is what produced the 13xx vmid races, a
foreign template squatting a golden slot, and the over-broad sweep that took out two other comps'
engines and goldens. To run two ranges deliberately, give each its own `--scoring-vmid` and
`TF_VAR_team_identifiers` blocks and set `TEZ_ALLOW_CONCURRENT=1`; the gate then warns instead of
refusing.

Teardown after a practice run is `destroy-competition.py` — it is resumable (stale-lock recovery,
tag-scoped leftover sweep, foreign VMs skip-and-continue); re-run it until it exits clean. **Never
substitute ad-hoc destroy scripts**: sweep predicates that match names or partial tags will destroy
other sessions' infrastructure (live-found 2026-09-30 — an over-broad sweep took out two other comps'
engines and goldens).

Teardown also collects this run's reports and evidence into
`competitions/<id>/.automated-tests/<run-id>/` **before** it destroys anything (red report pulled
from red01, blue logs sealed, engine capture, then `REPORT.md` with the machine sections filled and
the judgement sections marked `TODO(author)`). It warns and proceeds — a dead box never blocks a
destroy — and what it could not get is recorded in `collection.json` and in the report's caveats
rather than left as an empty folder; `--skip-artifacts` bypasses it entirely. **Because the
competition dir is per-worktree, archive before discarding the worktree:** `python3
test-artifacts.py archive <comp> --all`, or keep the copy teardown already made under
`~/.tezcatlipoca/automated-tests/` (it archives automatically when it detects a linked worktree, and
again on `test-artifacts.py verify --seal` once the write-up is finished).

A linked worktree has none of the gitignored local state the deploy reads, and every path resolves
from the worktree root — run all commands from there. Pre-flight:

```bash
git worktree add -b <branch> ../tezcatlipoca-<branch> main
cd ../tezcatlipoca-<branch>
git submodule update --init                          # vendor/nakon — every nakon call needs it
cp /path/to/main-tree/.env .env                      # the NODE-PROPER env variant (see below)
cp /path/to/main-tree/vendor/nakon/.env vendor/nakon/.env
cp /path/to/main-tree/proxmox . && chmod 600 proxmox # deploy resolves `../proxmox` against terraform/
```

- Pick the env file matching the target node and **check the stale-var traps**: on .150 that is
  `.env.cyberrange-20260930` (the MAIN `.env` targets the down .193 — copying it is the classic
  wrong-variant mistake; every banner now prints the resolved endpoint/node/datastore so a wrong
  copy shows immediately). The engine mgmt IP default (.250) is SSH-poisoned on .150's tailnet
  path — the cyberrange variant pins `TF_VAR_engine_mgmt_ip=10.0.0.252`, and the preflight now
  REFUSES an unverifiable default IP instead of proceeding. The
  `.env.realm-backup-20260923` (.150) variant shipped `TF_VAR_template_vm_id=9106` (dead vmid — the
  engine-base preflight hard-fails; correct value is **955**) and no `TF_VAR_team_identifiers`
  (default identifiers 101… collide with nothing by themselves, but on a shared node 100–124 are
  *all* occupied — set it explicitly, e.g. `TF_VAR_team_identifiers=130,131`). The main `.env`
  (targets .193) shipped `9088`, which exists on neither node — corrected to **1007** on 2026-10-02;
  for cyberfield the ubuntu fix template is **1007**. Re-check this line before trusting any env
  variant: a dead vmid here hard-fails the engine-base preflight, and every variant has carried one
  at some point. Correct template vmids by node: 955/1007 ubuntu, 951/1006 debian-lite, 1016/1015 fedora,
  127/1019 alpine — see [docs/environment-facts.md](docs/environment-facts.md#templates).
- `TF_VAR_teams` / `TF_VAR_boxes_per_team` in an old env are overridden by the comp dir at terraform
  time — stale values there are cosmetic, not fatal.
- `TEZ_THIN_HEADROOM=<0..1>` relaxes the datastore headroom gate on thin-provisioned pools (ZFS,
  lvmthin): the gate counts that fraction of the provisioned team-disk math, because linked clones
  only allocate written blocks (goldens + engine are the only full copies). Unset keeps the strict
  provisioned-bytes gate. Size the factor to the pool, not to hope: 0.25 has been enough for
  Windows-heavy comps on cyberrange `hdd`.
- The sibling-repo tools have their own env fallback: `bad-auto/deploy` loads `../tezcatlipoca/.env`
  via `setdefault`, so export the worktree's `TF_VAR_proxmox_*` trio before calling it if the main
  tree points at a different node.

Then the normal flow: `create-competition.py --competition <id> --scoring-vmid <free> --plan-only`,
real `--teams N --yes`, verify, destroy — all from the worktree root. Worked example:
[svc-matrix-2026-09-28-report.md](docs/reports/svc-matrix-2026-09-28-report.md).

## Editing and dispatch discipline

Cheap habits that the session logs show being re-learned expensively:

- **Read a file before editing it**, and prefer a small anchored edit over a large literal block.
  The single most common tool failure in the recorded sessions is `File has not been read yet`
  (40 times), followed by `File has been modified since read` (30) — the latter is concurrency, not
  carelessness, so when several sessions share a tree expect it and re-read.
- **No mutating shell commands from plan mode.** 36 permission denials, all `mode.plan.nonReadOnly`.
- **Never re-dispatch an identical subagent task** after it fails. One session lost the same task
  four times; a failed delegation is not a reason to send the same prompt again. Prompts should name
  the artifact the subagent must produce, so a retry can see what already exists.
- **Long operations are detached** — see
  [usage-agents.md → Running a deploy that outlives your shell](docs/usage-agents.md#running-a-deploy-that-outlives-your-shell).
  Never wrap a deploy in `timeout` to make it fit a tool call.
- **Kill by pid or process group**, never `pkill -f` a pattern that also appears in the invoking
  command line.
- **Never judge a command's success through a pipe** — `cmd | tail` reports the PIPE's exit code,
  so a failed pytest/deploy reads as success (live: a red suite was briefly committed, 2026-10-04).
  Check `${PIPESTATUS[0]}`, or redirect to a file and echo `$?` from the command itself.
- **Never write "validated" in a commit message before it is.** State what was done and what the
  next step proves; the report/docs are where validated claims live after they pass.

## Editing docs

Docs are held to the same standard as code: **verify every claim against the source before writing
it** (the pipeline is generation "v2" — golden templates + linked clones — and passages written
against the pre-golden pipeline are still being found), prefer one canonical statement plus links
over copies. If a doc and the
code disagree and the code looks like the bug, document the code's actual behavior and flag the
discrepancy (`docs/known-issues.md`) rather than silently "fixing" the doc. Fixed issues are
deleted from `docs/known-issues.md`, not archived — git history
is the record. Never edit code from a
docs pass.
