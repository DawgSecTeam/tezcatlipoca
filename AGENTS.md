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
- [docs/remote-access.md](docs/remote-access.md) — headscale participant access (engine-as-router, per-team scoping, `TEZ_HEADSCALE_*` env, roster)
- [docs/known-issues.md](docs/known-issues.md) — **open/pending issues only; a temporary
  writepad drained every run cycle — nothing lives here permanently**
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
collisions). A state file with no run id is refused by teardown outright, and untagged VMs are
never destroyed unless they carry this competition's clone marker. Details:
[docs/usage-agents.md](docs/usage-agents.md#run-ownership-teardown-only-touches-this-deploys-vms-2026-10-02).

**Concurrent ranges are supported, from isolated worktrees.** Nothing warns when another deploy is
live against the same estate. The preflight refuses REAL collisions wherever they can be proven
(vmid clashes, engine mgmt-IP conflicts), and a second deploy against the same scoring engine is
refused by the engine flock (`acquire_engine_lock`, `~/.tezcatlipoca/locks/`). Safe coexistence is
otherwise on you: give each range its own `--scoring-vmid` and `TF_VAR_team_identifiers` blocks, and
let the run-id ownership tags scope each teardown to its own deploy's VMs. Sessions sharing vmid
blocks produced the 13xx vmid races, a foreign template squatting a golden slot, and the over-broad
sweep that took out two other comps' engines and goldens.

**bad-auto state is per-competition — nothing is shared but the code.** red01's config, deploy
stamp and identity live in `competitions/<id>/` (`.badauto-config.yaml` + the stamp bad-auto
writes next to it), its vmid is reserved per comp (Compfile `red01_vmid`, else derived
`<scoring-vmid>+170`) and its IP pinned (`red01_ip`, else 10.0.0.198) — both preflight-GATED
like the engine's: an occupied red01 slot is a REFUSAL naming the owner, never a silent default
onto vmid 999 (that default shared across sessions is the 2026-10-12 red01 kill; see
[docs/bad-auto-isolation.md](docs/bad-auto-isolation.md)). Two worktrees may deploy ranges with
red01s concurrently against the one shared bad-auto checkout with zero coordination.

Teardown after a practice run is `destroy-competition.py` — the thin CLI over the teardown
engine (`teardown/`, [docs/teardown-redesign.md](docs/teardown-redesign.md)). It is resumable by
construction (a completion journal skips what a prior pass finished; a warned step is retried by
re-running), and its exit 0 means the estate is VERIFIED clean for this deploy's ownership set —
nothing of ours running or left tagged, red01 gone, no terraform state lock, evidence archived.
**Never substitute ad-hoc destroy scripts**: there is no other path that can destroy a VM — every
destroy-shaped API takes the deploy's `Ownership` (comp tag + per-deploy run id) and refuses
anything less (live-found 2026-09-30 — an over-broad sweep took out two other comps' engines and
goldens; the guarded path is the only path now). Every teardown path force-stops (status/stop) the
VMs it owns before deleting — never graceful ACPI shutdowns: a doomed VM gains nothing from grace
and agent-less boxes hang it, so graceful shutdown is only for VMs that survive the operation
(2026-10-01).

Teardown collects this run's reports and evidence into
`competitions/<id>/.automated-tests/<run-id>/` **before** it destroys anything (red report pulled
from red01, blue logs sealed, engine capture, then `REPORT.md` with the machine sections filled and
the judgement sections marked `TODO(author)`). It warns and proceeds — a dead box never blocks a
destroy — and what it could not get is recorded in `collection.json` and in the report's caveats
rather than left as an empty folder. (There is no `--skip-artifacts`: on a resume the journal
marks collection done and skips it.) **Because the competition dir is per-worktree, archive
before discarding the worktree:** `python3 test-artifacts.py archive <comp> --all`, or keep the
copy teardown already made under `~/.tezcatlipoca/automated-tests/` (it archives automatically
when it detects a linked worktree, and again on `test-artifacts.py verify --seal` once the
write-up is finished).

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
  `.env.cyberrange-20260930`; the main `.env` targets cyberfield .193 (which has had hardware
  instability, so verify it is reachable before deployment). Every banner prints the resolved
  endpoint/node/datastore so a wrong copy shows immediately. The engine mgmt IP default (.250) is SSH-poisoned on .150's tailnet
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
- All competition ops on Proxmox run on the `terraform@pam!comp-automation` token — never the
  workshop portal's `root@pam!guac-provisioner` (user directive 2026-10-09). When copying an env
  variant across nodes, re-export `TF_VAR_proxmox_api_token` explicitly instead of inheriting
  whatever the source file carried; check it before any deploy/destroy.
- `TF_VAR_teams` / `TF_VAR_boxes_per_team` in an old env are overridden by the comp dir at terraform
  time — stale values there are cosmetic, not fatal.
- `TEZ_THIN_HEADROOM=<0..1>` relaxes the datastore headroom gate on thin-provisioned pools (ZFS,
  lvmthin): the gate counts that fraction of the provisioned team-disk math, because linked clones
  only allocate written blocks (goldens + engine are the only full copies). Unset keeps the strict
  provisioned-bytes gate. Size the factor to the pool, not to hope: 0.25 has been enough for
  Windows-heavy comps on cyberrange `hdd`.
- The sibling-repo tools have one read-only shared fallback: `bad-auto/deploy` loads
  `../tezcatlipoca/.env` (the MAIN tree's) via `setdefault`, so export the worktree's
  `TF_VAR_proxmox_*` trio before calling bad-auto by hand if the main tree points at a different
  node. (Deploy identity is NOT shared — it lives in each comp's own
  `competitions/<id>/.badauto-config.yaml`.)

Then the normal flow: `create-competition.py --competition <id> --scoring-vmid <free> --plan-only`,
real `--teams N --yes`, verify, destroy — all from the worktree root. Worked example:
[svc-matrix-2026-09-28-report.md](docs/reports/svc-matrix-2026-09-28-report.md).

## Editing and dispatch discipline

Cheap habits that the session logs show being re-learned expensively:

- **Fix causes, not symptoms — this is the project's first discipline.** A test
  run exists to find defects so they can be killed at the root: the goal is a
  stack that runs end-to-end with no agent intervention, not a run that an
  agent shepherds past its failures. Diagnose from evidence before retrying; a
  failure you cannot explain is not resolved, and "flaky" means broken. Every
  fix lands in code with a regression test. No workarounds during a run (no
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
  The single most common tool failure in the recorded sessions is `File has not been read yet`
  (40 times), followed by `File has been modified since read` (30) — the latter is concurrency, not
  carelessness, so when several sessions share a tree expect it and re-read.
- **No mutating shell commands from plan mode.** 36 permission denials, all `mode.plan.nonReadOnly`.
- **Never re-dispatch an identical subagent task** after it fails. One session lost the same task
  four times; a failed delegation is not a reason to send the same prompt again. Prompts should name
  the artifact the subagent must produce, so a retry can see what already exists.
- **Long operations are detached** — see
  [usage-agents.md → Running a deploy that outlives your shell](docs/usage-agents.md#running-a-deploy-that-outlives-your-shell).
  Never wrap a deploy in `timeout` to make it fit a tool call, and never carry an hour-scale run in
  a background tool task: the harness reaps task records (live: a scrim driver died at T+92 of 180
  when its task record vanished) — `setsid nohup` plus log polling instead.
- **Push after changes** (user directive 2026-09-29): committing is not finishing — push to origin
  as part of the task. Fetch and check divergence first; parallel sessions push to main too, so
  rebase rather than force-push (force-push is for sanctioned history rewrites only, like the
  2026-10-13 squash — never casually). Never push anything carrying live secrets.
- **One commit per branch** (user directive 2026-10-13): a finished branch lands on main as a
  SINGLE squash commit with a brief one-line message — `git merge --squash <branch>` + commit,
  never a merge commit carrying the branch's incremental train. main's history was rewritten to
  this shape on 2026-10-13 (old history: local ref `backup-pre-squash-20261013`), so stale
  worktrees must NOT `git pull` — finish and squash-merge, or re-cut the branch.
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
  so a failed pytest/deploy reads as success (live: a red suite was briefly committed, 2026-10-04).
  Check `${PIPESTATUS[0]}`, or redirect to a file and echo `$?` from the command itself.
- **Never write "validated" in a commit message before it is.** State what was done and what the
  next step proves; the report/docs are where validated claims live after they pass.

## Code style

**Minimum code (user directive 2026-10-12, standing):** write the minimum code that solves the
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
