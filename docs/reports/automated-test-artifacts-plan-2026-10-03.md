# Automated test artifacts — per-run reports inside each competition (plan, 2026-10-03)

**Status: IMPLEMENTED 2026-10-03 — this document is the design record.** The canonical contract
(what a test folder contains, who writes it, how to read it) is now
[automated-test-artifacts.md](../automated-test-artifacts.md); the code is
[artifacts_ops.py](../../artifacts_ops.py) + [test-artifacts.py](../../test-artifacts.py), with the
teardown hook in [destroy-competition.py](../../destroy-competition.py) and the harness integration
in [run-agent-scrim.py](../../run-agent-scrim.py). Decisions taken while implementing are recorded in
§8. Original framing: the fleshed-out design for "every automated test run leaves a standard folder of
reports under the competition it tested". Every claim below was checked against source in a survey on
2026-10-03 at `6b55d76` — line numbers drift, so re-read before editing.

Underlying source survey (gitignored, ~85 KB of `path:line` citations):
`logs/at-design/01-scrim-model.md`, `02-teardown-pull.md`, `03-report-formats.md`.

---

## 0. Verdict on the shape as proposed

The four structural decisions are right and should be kept:

| Proposed | Verdict |
|---|---|
| Anchor artifacts in `competitions/<id>/` | **Keep.** It is where everything else about a comp already lives, and `webui/server.py:37` already enumerates `competitions/*`. |
| One folder per test | **Keep.** The current alternative is `<comp>`-keyed, so two runs collide by design (`run-agent-scrim.py:2146`), which is why a directory had to be hand-named `cde-2026-run2`. |
| Three canonical documents (red / blue / full) | **Keep** as the human interface, and keep the names stable. |
| Red report pulled by teardown, not handed over by the agent | **Keep the intent**, and it is *more* achievable than proposed: the red report is already a tool-generated artifact, not something the agent authors — red's director writes `report-<ts>.md` into its state dir at run exit, best-effort (`badauto/report.py:325-329`, called from `badauto/brain/director.py:812-816`). The harness itself never invokes `badauto report`; the on-box file is what exists. |

Five things need to change before this is buildable:

1. **Key the folder on the run id that already exists** (`run-<8hex>`, `utils.py:64-71`) instead of a
   free-form test name. It is minted per comp dir, persisted in `.deploy_state.json`, stamped on every
   VM as an ownership tag (`constants.py:186-198`), reused across resume/redeploy, and already required
   by teardown. Folder identity then correlates with VM identity for free, and two worktrees running
   the same competition cannot collide.
2. **Make the test folder the harness's run directory from the start**, rather than collecting its
   output afterwards. The harness already produces `run.json`, `T0.txt`, `scoreboard-state.jsonl`,
   `blue-team*/`, `evidence/`; pointing `--run-dir` at the comp dir (`run-agent-scrim.py:2146`,
   `:2102`) converts a collection problem into a placement problem. "Pull info from each run" should
   only apply to what genuinely lives elsewhere.
3. **"Pull the blue report off the box" is not implementable as stated.** Blue never runs on a guest:
   the blue agents are local `opencode` cycles in `<run_dir>/blue-team<n>/` reaching boxes only through
   a `mybox` ssh helper (`run-agent-scrim.py:821-841`, `:1150-1156`). And red01 is *not* a
   tezcatlipoca VM — it is deployed and destroyed by sibling `bad-auto` at `10.0.0.198`
   (`run-agent-scrim.py:1945-1948`), so `destroy-competition.py` can neither enumerate nor own it.
   The honest split is: **pull** (red01 evidence + report), **capture** (engine-side blue deliverables
   before the scoring DB dies), **seal** (operator-side blue files, hashed and chmod'ed).
4. **Three prose files are not a pull interface.** Add two machine files (`test.json`,
   `collection.json`) plus `evidence/`, or every consumer has to parse markdown, and a folder cannot
   distinguish *"the agent wrote nothing"* from *"the pull failed"* from *"the VM was already gone"*.
   That distinction is the whole point of doing this at teardown.
5. **Decide durability and secrecy before writing code.** A practice run must happen in a throwaway
   worktree (AGENTS.md), and the comp dir is per-worktree — `teams.json` is gitignored and produced by
   the deploy (`deploy.py:1019`), so teardown only works from the worktree that deployed. Artifacts
   therefore die with the worktree unless archived. Conversely, nothing under `competitions/<id>/` is
   gitignored today, so box-pulled reports are committable by default.

---

## 1. What exists today (the design has to fit this, not replace it)

**Three artifact homes, none run-keyed.**

| Home | Written by | Keyed on |
|---|---|---|
| `competitions/<id>/evidence/event-final-<ts>.json` | `run-schedule.py:101,135-138` | timestamp |
| `/home/hna/dev/dawgsec/scrim-runs/<competition>/` — `run.json`, `T0.txt`, `scoreboard-state.jsonl`, `monitor.log`, `blue-team<n>/`, `evidence/` | `run-agent-scrim.py:2146-2171` | competition only |
| `/var/lib/bad-auto/{events.jsonl,world.json,report-<ts>.md,report-secrets.md}` on red01 | bad-auto (`badauto/report.py:325-329`) | red01 state dir |

**The mechanical pass/fail already exists and must not be reinvented.** `docs/rehearsal-gates.md`
defines red and blue gates; `scrim-report.py` evaluates them and prints a verdict
(`GREEN | NOT READY | FAILED RUN | NO INTERACTION`) plus a gate table into `INTERACTION.md`
(`scrim-report.py:430-539`). The synthesis report should *consume* that verdict, never restate a
second opinion of the same numbers.

**The lifecycle has two destroys, in this order** (`run-agent-scrim.py:1935-1954`):

```
stage_run → stage_capture (:1810-1811) → stage_teardown:
    stop LLM tunnel → pull_red_evidence (:1942) → badauto destroy  [kills red01 + NAT] (:1947)
    → destroy-competition.py                                      [kills the range] (:1953)
→ operator writes FINDINGS.md by hand (:2177)
```

Two consequences: (a) red01 is gone *before* `destroy-competition.py` starts, so a collector that
lives only in teardown will find nothing after a normal run; (b) if the harness dies mid-run, nothing
collects at all — the range is simply left up. That second case is the one worth building for:
`destroy-competition.py` is the script AGENTS.md tells you to re-run until clean, so it is the only
step guaranteed to happen.

**There is no guest file-pull helper in this repo.** The real primitives are:

- `range_ops.guest_agent_exec_root(node, vmid, script, timeout=60, shell="bash")` → `(rc, stdout, stderr)`, node-routed over the PVE API token (`range_ops.py:213-231`);
- `range_ops.guest_agent_exec_windows(node, vmid, ps_script, timeout=120)` (`range_ops.py:234-256`);
- `ssh_ops.ssh_via_gateway(ctx, target_ip, cmd)` — SSH through the engine hop (`ssh_ops.py:91-109`);
- PVE `agent/file-read` (returns already-decoded `content`) — used only by the external skill CLI
  (`~/.agents/skills/proxmox-ops/scripts/pmx.py:25,591`), no call site in this repo;
- the existing red01 pull: `scp` direct, then via an engine-jump `ProxyCommand`, with `returncode == 0
  && size > 0` verification and partial-file cleanup (`run-agent-scrim.py:1896-1932`).

**Box enumeration exists and must be used instead of name patterns.** `range_ops.load_targets(comp_dir,
teams, boxes)` (prefers the deploy-frozen `targets.json`) and `range_ops.enumerate_targets(...)`
(`range_ops.py:451-476`, `:644-676`) give `team_key/identifier/box_name/vmid/ip/vm_name/node/slot`.
Do **not** copy the `f"{identifier}-{box}"` convention used by `pre_stop_windows_boxes`
(`destroy-competition.py:162`): terraform names team 1's VMs `team1-<box>`, so that filter silently
never matches team 1.

**Secrets.** `tests/test_secret_hygiene.py` scans **tracked files only** (`git ls-files`, `:79-83`) and
its broadest rule is a line-anchored assignment — `TF_VAR_*password=` / `TF_VAR_*token=`, or an
all-caps `*PASSWORD=` / `*SECRET=` / `*TOKEN=` — whose value is ≥16 chars (`:55-71`). It is a
shape-matching net, not a publish guarantee: a password named in prose or in a table is not caught —
`docs/reports/amongus-cde-2026-report.md:181` quotes live spec passwords in a tracked file today. `.gitignore` has no rule for `competitions/*/evidence/`, `…/LOG.md`,
`…/INTERACTION.md`, `…/*report*.md`. A tracked machine artifact precedent does exist and is
credential-free: `competitions/same-type-2box/coverage-run1.json`.

**Run id is not present on disk anywhere yet.** All seven `competitions/*/.deploy_state.json` predate
it (`run_id=ABSENT`), so the first implementation must handle a null run id.

---

## 2. The standard format

```
competitions/<comp>/
  .automated-tests/                    # gitignored drop point (see §6)
    index.json                         # roll-up: one record per test, newest first
    <run-id>/                          # e.g. run-1a2b3c4d, or untagged-<ts> when state has no run id
      test.json                        # identity + intent + verdict + writeup status  (machine, 0600)
      collection.json                  # what was pulled/captured/sealed, from where, hashes, failures
      RED-TEAM.md                      # pulled from red01 by teardown (or regenerated operator-side)
      BLUE-TEAM.md                     # blue's authored report, sealed by teardown (hash + 0600)
      REPORT.md                        # synthesis: incidents, run success, recommendations
      evidence/
        red/                           # /var/lib/bad-auto/{events*.jsonl,world.json,report-*.md,journal}
        engine/                        # final scoreboard + /api/injects + per-team services dumps
        blue/                          # LOG.md, NOTEBOOK.md, feed.log, cycles/, sub-*.md, submissions/
        harness/                       # run.json, T0.txt, monitor.log, watchdog.log, deploy log,
                                       #   INTERACTION.md, alerts.jsonl, .deploy-timings.jsonl copy
```

Rules that make it a *standard*:

- **Fixed filenames, always present in a completed test.** A missing red/blue report is represented by
  an explicit `RED-TEAM.md` containing a provenance stub that says why it is missing (no agent in this
  run / pull failed / VM already destroyed) — never by an absent file, which is indistinguishable from
  a wiring bug.
- **`kind` is explicit**: `scrim | deploy | soak | canary | loadtest | rehearsal`. Not every test has
  agents (`agents.red.present=false`), and the report skeleton must not imply coverage it did not have.
- **Machine facts live in JSON, prose lives in markdown.** No fact appears as the only copy in prose.
- **Every collected file carries `sha256` + `bytes` + `remote` path + `route`** in `collection.json`.
- **Docs carry a minimal front-matter block** (id, kind, run, generated, author) so a file copied out
  of its folder is still self-identifying. This is the one place front-matter is introduced; the repo's
  existing reports have none (`docs/reports/*.md` all start with `# `).

### `test.json` (sketch)

```json
{
  "schema": 1,
  "comp": "agent-scrim-2026-09-17b",
  "comp_dir": "competitions/agent-scrim",
  "run_id": "run-1a2b3c4d",
  "kind": "scrim",
  "label": "2-team 90-min red-vs-blue",
  "created_at": "2026-10-03T01:22:00-04:00",
  "created_by": {"script": "run-agent-scrim.py", "git_rev": "6b55d76",
                 "worktree": "/home/hna/dev/dawgsec/tezcatlipoca-at", "dirty": false},
  "endpoint": "https://10.0.0.193:8006", "node": "pve", "nodes": ["pve"],
  "teams": 2, "boxes": ["dc01", "win01", "web01", "app01", "db01"],
  "agents": {"red":  {"present": true, "ip": "10.0.0.198", "vmid": 999, "node": "pve", "model": "…"},
             "blue": {"present": true, "teams": 2, "model": "…"}},
  "event": {"t0": 1790000000, "duration_min": 90},
  "verify": {"exit_code": 0, "args": ["--strict-services"], "log": "evidence/harness/verify.log"},
  "verdict": {"status": "GREEN", "score": 11, "gates_passed": 9, "gates_failed": 1,
              "gates_na": 3, "source": "evidence/harness/INTERACTION.md"},
  "writeup": {"status": "needs-writeup", "author": null, "completed_at": null},
  "teardown": {"at": null, "exit": null, "cloned_vms_archived": null}
}
```

`red.vmid`/`red.ip` are recorded **at `stage_red` time**, not read back later: bad-auto's
`config.yaml` is a mutable singleton rewritten by each launch, so `verify-competition.py:827`'s habit
of reading it is fine for a fresh check but wrong at teardown — it can point at another run's red01.

### `collection.json` (sketch)

```json
{"schema": 1, "run_id": "run-1a2b3c4d", "collected_at": "…",
 "collector": {"script": "destroy-competition.py", "git_rev": "…"},
 "targets": [
   {"name": "red01", "node": "pve", "vmid": 999, "route": "scp+jump", "status": "ok",
    "files": [{"remote": "/var/lib/bad-auto/report-20261003-0122.md",
               "local": "evidence/red/report-20261003-0122.md", "bytes": 8123, "sha256": "…"}]},
   {"name": "engine", "node": "pve", "vmid": 1500, "route": "quotient-api", "status": "ok", "files": []},
   {"name": "blue-team1", "route": "local-seal", "status": "sealed", "files": []},
   {"name": "team1-web01", "node": "pve", "vmid": 1220, "route": "guest-agent",
    "status": "skipped", "reason": "no guest-side artifact defined for this run kind"}],
 "derived": [{"path": "RED-TEAM.md", "from": "evidence/red/report-20261003-0122.md",
              "method": "pulled", "sha256": "…"}],
 "summary": {"ok": 3, "absent": 0, "failed": 0, "unreachable": 0, "unrecoverable": 0, "skipped": 1}}
```

Status vocabulary, and it must stay this small:
`ok | absent` (reachable, file genuinely not there) `| failed` (reachable, transfer failed) `|
unreachable` (VM/agent down) `| unrecoverable` (VM already destroyed) `| sealed` (local, hashed) `|
skipped`.

That vocabulary is what makes the folder honest and is the reason `collection.json` is not optional:
"red report is missing" must be readable without asking the operator whether they forgot a step.

---

## 3. Plumbing

### 3.1 One implementation, two callers

New module **`artifacts_ops.py`** (house style: `config_ops`, `range_ops`, `ssh_ops`, …) holding the
whole lifecycle, plus a read-only CLI **`test-artifacts.py`** (`list`, `show`, `verify <run-id>`,
`archive`). Both the scrim harness and teardown import the module — never a second copy of the scp
logic (this repo has `tests/test_helper_dedup.py` precisely because divergent helpers keep happening).

```
artifacts_ops.py
  test_dir(comp_dir, run_id, kind) -> Path          # <comp>/.automated-tests/<run-id>
  load_manifest / save_manifest(test.json)          # via config_ops.write_state (atomic, 0600)
  ensure_test(comp_dir, run_id, **fields)           # idempotent create + index refresh
  collect(comp_dir, run_id, targets, routes, ...)   # pull + capture + seal, writes collection.json
  render_report_skeleton(test_dir)                  # REPORT.md with machine sections filled
  render_missing_stub(side, status, reason)         # RED-TEAM.md / BLUE-TEAM.md stub
  verify_test(test_dir) -> drift report             # re-hash, re-check presence
```

New primitives, added next to the existing agent helpers in `range_ops.py`:

- `guest_file_read(node, vmid, path, timeout=60)` — preferred implementation `agent/file-read`
  (decoded `content`), fallback base64-`cat` through `guest_agent_exec_root`, Windows branch through
  `guest_agent_exec_windows`. Model the "probe the platform first" pattern on `ssh_ops.wait_for_boxes_ssh`
  (`ssh_ops.py:160-181`).
- Keep `scp` + engine-jump for red01 (that is the only proven route to it, `run-agent-scrim.py:1906-1914`).

### 3.2 Hook points

**Teardown — the load-bearing hook.** Insert one step between `destroy-competition.py:485` and `:487`
(immediately before the `cloned_vms_path` / `destroy_cloned_vms` block). At that point `comp_dir`,
`teams`, `boxes`, `run_id`, `placement`, `team_nodes` and the activated node routes are all known
(`:399-416`, `:448`), and nothing has been stopped or deleted yet — the first destructive call is
`:489`, and `pre_stop_windows_boxes` (`:490`) hard-stops every team clone, after which the agent
channel is dead. A Ctrl-C there leaves the range intact.

```
... :485  env = {...}
+         collected = artifacts_ops.collect_for_teardown(comp_dir, teams, boxes, run_id,
+                                                    placement=placement, team_nodes=team_nodes)
+         if collected.blocking and not (args.yes and args.force_collect):
+             sys.exit("refusing to destroy: … re-run with --force-collect to proceed")
  :487  cloned_vms_path = comp_dir / "cloned_vms.json"
```

**Harness.** Three small changes in `run-agent-scrim.py`:

- `--run-dir` defaults to `artifacts_ops.test_dir(comp, run_id, "scrim")` instead of
  `scrim-runs/<competition>` (`:2102`, `:2146`); `run.json` becomes/feeds `test.json`.
- `stage_red` records `red_ip`/`red_vmid`/`red_node` into `test.json` (`:1666-1691`).
- `stage_teardown` calls `artifacts_ops.collect(...)` where `pull_red_evidence` is called today
  (`:1942`), i.e. **before** `badauto destroy` (`:1947`); keep `pull_red_snapshot` for in-run
  snapshots (`:1403-1422`) — different purpose, do not merge.

**Engine capture.** `stage_capture` already dumps `/api/teams`, `/api/injects`, per-team services and
the final scoreboard before anything destructive (`:1815-1842`). Two gaps to close while wiring this:
the capture should land in `evidence/engine/`, and it must include the **inject submission bodies** —
today the harness's blue copy globs `sub-*.md`/`sub-*.txt` (`:1887`) while the prompt tells blue to
write `sub.md` (`:947`), so observed runs captured `sub.md` and counted 0 injects with an empty
`submissions/`. Blue's scored deliverable is on the engine, not in the workdir.

### 3.3 Ordering rules (each one is a way this feature silently loses data)

1. Collect **after** confirmation, **before** the first stop/delete.
2. Red01 pull happens **before `badauto destroy`**, in both callers.
3. Engine capture happens **before** the scoring DB is destroyed (`stage_capture` already respects this
   and says so at `:1821-1822`).
4. Report *generation* may happen after destruction; report *collection* never may.

### 3.4 Idempotency

Destroy is expected to be re-run until clean (`:502-505`), therefore:

- `collection.json` is the progress record — no new comp-global state file. On re-run, targets already
  `ok`/`sealed` are skipped and re-verified by hash, not re-pulled.
- Never overwrite a good artifact with a worse one: copy the existing pull discipline (verify rc,
  existence, non-zero size; unlink the partial on failure) (`run-agent-scrim.py:1908-1914`).
- All writes go through `config_ops.write_state` / `write_text_atomic` (atomic + mode at creation,
  `config_ops.py:767-796`); pulled files are `chmod 0600` like `secure_evidence`
  (`run-agent-scrim.py:328-331`).

### 3.5 No gate: warn loudly and proceed

Collection never blocks a destroy (operator decision, 2026-10-03 — a dead box must not wedge a
teardown). What shipped instead:

- the collector prints every gap as `WARNING: <document> is <STATUS> although this run had a <side>
  agent …`, records it in `collection.json`, and restates it in `REPORT.md`'s caveats;
- `--skip-artifacts` bypasses collection entirely for a teardown that needs to be fast;
- `--artifacts-timeout` (default 45s) bounds each transfer, and a target is dropped after its first
  unreachable transfer, so a dead red01 costs one timeout rather than five;
- the whole step is wrapped so a collector crash warns and continues — the range still gets destroyed.

---

## 4. `REPORT.md` — the synthesis the user actually asked for

The repo has already converged on a skeleton; reuse it instead of inventing one. Common spine across
the five surveyed reports: title → environment (node, worktree, branch, rev) → verdict → lineup →
deploy narrative → verify gate table → findings with severity → recommendations → teardown →
not-verified/caveats. The soak variant (`scrim-runs/scale8-scrim-2026-10-01/REPORT.md`) adds a timing
breakdown sourced from the timing sidecar and an **Artifacts index**, and is the closest existing
template.

Teardown writes the skeleton with every machine section filled and judgement sections marked:

```markdown
---
id: run-1a2b3c4d   kind: scrim   comp: agent-scrim-2026-09-17b
generated: 2026-10-03T02:10:00-04:00   author: artifacts_ops.py
---

# <kind> report — <comp> — <run-id> — 2026-10-03

## Verdict
<!-- machine: copied verbatim from test.json.verdict / INTERACTION.md. Do not restate numbers. -->
GREEN — interaction score 11; gates 9 pass / 1 fail / 3 n/a (source: evidence/harness/INTERACTION.md)

## Environment
<!-- machine -->
node pve · endpoint … · worktree … @ 6b55d76 (clean) · teams 2 · boxes … · templates/hashes …

## Timeline and cost
<!-- machine: run.json phases + .deploy-timings.jsonl + evidence/harness/deploy.log -->
T0 … ; deploy … min; phase table; where the wall clock went.

## Incidents
<!-- machine seeds the list from evidence/harness/{monitor,watchdog}.log + alerts.jsonl +
     utils.degradations() + verify failures + collection.json failures; AUTHOR must classify. -->
| when | what | impact | resolved | evidence |
<!-- TODO(author): classify each, add anything only a human saw -->

## Red
<!-- machine: metrics from INTERACTION.md; narrative from RED-TEAM.md; provenance from collection.json
     (pulled / regenerated / missing-unrecoverable) -->
## Blue
<!-- machine: cycles rc=0, restorations, TTR, injects, notebook entries; narrative from BLUE-TEAM.md -->

## Recommendations — this competition (<comp>)
<!-- TODO(author) -->
## Recommendations — tezcatlipoca
<!-- TODO(author) -->
<!-- fixed heading, one bullet per item, each with: symptom → evidence path → suggested owner/file -->
## Not verified / caveats
<!-- machine seeds: skipped targets, n/a gates, unrecoverable artifacts, degraded steps -->
```

Two deliberate choices:

- **The tool-recommendations section is a fixed heading.** That is the harvest path into
  `docs/known-issues.md` / a fixes plan — the repo already works this way (FINDINGS.md → a plan doc →
  code). Make it greppable rather than hoping a reader re-reads the prose.
- **`writeup.status` gates "done".** Teardown leaves `needs-writeup`; filling the judgement sections
  and running `test-artifacts.py verify <run-id> --seal` flips it to `done` (refuses while `TODO(author)`
  remains, records author + timestamp, re-hashes everything, refreshes `index.json`). The "full report"
  is authored — the plan should say so out loud rather than pretend a script wrote the recommendations.

---

## 5. Where the pulls actually come from

| Artifact | Source | Route | Notes |
|---|---|---|---|
| `RED-TEAM.md` | `/var/lib/bad-auto/report-<ts>.md` on red01 | scp direct, else engine-jump (`run-agent-scrim.py:1906-1914`) | Written best-effort by red's director at run exit (`badauto/brain/director.py:812-816`), so it is simply absent when red was hard-killed. Pick the **newest** match and record every match: a mid-event red restart leaves several. |
| red evidence | `/var/lib/bad-auto/{events.jsonl,world.json,report-secrets.md,intel/}` + `journalctl -u bad-auto` | same | `report-secrets.md` is 0600 and holds the credlist restore table — keep it under `evidence/red/`, never in the shareable report. |
| `RED-TEAM.md` fallback | regenerate from pulled `events.jsonl` + `world.json` with `badauto report --out` against the pulled state dir (`badauto/cli.py:120-148`) | operator-side | Needed whenever red was killed before its director wrote the report. Record `method: "regenerated"`. |
| engine deliverables | `/api/injects` + submission bodies + final `/api/services/<tid>` | Quotient API via existing `qget`/curl | Must precede engine destruction. Closes the `sub.md` vs `sub-*.md` gap. |
| `BLUE-TEAM.md` | `<run_dir>/blue-team<n>/{LOG.md,NOTEBOOK.md,feed.log,cycles/,sub-*.md,submissions/}` | local **seal** (copy + hash + 0600) | Not a guest pull. If a genuine guest-side blue artifact is wanted, blue's prompt must name a fixed on-box path *and* the collector must be told it — a separate decision (§7). |
| machine facts | `run.json`, `T0.txt`, `scoreboard-state.jsonl`, `INTERACTION.md`, `alerts.jsonl`, `.deploy-timings.jsonl`, verify log | local copy into `evidence/harness/` | Deploy stdout is currently captured only on failure (`run-agent-scrim.py:391-392`) — capture the launch log in the test folder so phase timeline is not reconstructed from scrollback. |

---

## 6. Durability, secrecy, concurrency

**Durability.** `.automated-tests/` is the *drop point* (relative to the comp, so harness, teardown and
the webui all find it the same way). Because practice runs are worktree-scoped, the collector also
copies the finished test folder to a durable archive outside the repo —
`${TEZ_ARTIFACTS_ARCHIVE:-~/.tezcatlipoca/automated-tests}/<comp>/<run-id>/` — automatically when the
repo root is a linked worktree (`git rev-parse --git-dir` ≠ `--git-common-dir`), or on explicit
`test-artifacts.py archive`. Print the archive path at teardown, and add one line to the AGENTS.md
practice-run section: *before `git worktree remove`, run `test-artifacts.py archive <comp>` and keep
what it printed.* Otherwise the first discarded worktree eats the first month of test history.

**Secrecy.** Land `.gitignore` + test enforcement together:

```
competitions/*/.automated-tests/
```

and a rule in `tests/test_secret_hygiene.py` that **no tracked path may live under
`competitions/*/.automated-tests/`** — the same belt-and-braces style as its `.env` check (`:108-118`).
A `.gitignore` line alone is not enough in this repo: `competitions/pfsense-ad/terraform/terraform.tfstate`
is tracked today despite `competitions/*/terraform/` being ignored. **No publish path shipped**
(operator decision, 2026-10-03): a recommendation leaves this folder by being written into
`docs/known-issues.md` or a fixes plan, never by copying the artifact into git — a "publish the clean
ones" escape hatch is exactly how a credential reaches a public remote six weeks later.

**Concurrency.** Namespacing per `run-id` is what makes two worktrees on the same competition ID safe
(the 2026-10-02 near-miss). The collector reads its own comp dir's `teams/boxes/targets` and treats a
non-matching ownership tag as *skip, do not read* — it is reading, not destroying, so it must stay
harmless under a foreign run. `--legacy-tags` / `--allow-untagged` must not widen collection: with no
run id the test folder is `untagged-<YYYYmmdd-HHMMSS>` and collection is comp-scoped, best-effort, and
labelled as such in `test.json`.

---

## 7. Implementation order

Each step is independently verifiable; steps 1–2 are pure offline work, 5–7 are docs/tests.

1. **`artifacts_ops.py`** — folder/`test.json`/`collection.json` schemas, status vocabulary, index,
   sealing and hashing, `render_report_skeleton`, `render_missing_stub`. Unit tests:
   `tests/test_artifacts_ops.py` with a fake test dir (no estate access).
2. **`test-artifacts.py`** CLI — `list`, `show`, `verify <run-id> [--seal]`, `archive`.
3. **Harness placement** — `--run-dir` default, `test.json` identity at stage start (fold
   `record_phase`'s manifest into it), red identity recorded in `stage_red`, engine capture into
   `evidence/engine/` incl. submission bodies, blue copy globs fixed to match the prompt.
4. **Teardown hook** — the `:485/:487` insertion, red01 pull moved behind `artifacts_ops.collect`,
   engine capture, blue seal, `collection.json`, the gate + `--force-collect`.
   Regression tests in the style of `tests/test_scrim_defects.py`: *collection is called before
   `badauto destroy`* and *before the first stop/delete*; *a second run does not re-pull*.
5. **Report skeleton + seal loop** — `REPORT.md` generation, `--seal`, `index.json` refresh.
6. **Hygiene + durability** — `.gitignore` rule, `test_secret_hygiene.py` assertion, archive-on-worktree,
   AGENTS.md line.
7. **Docs** — new evergreen `docs/automated-test-artifacts.md` (the contract: layout, statuses, who
   writes what, how to read a test), rows in `docs/README.md`, and updates to `docs/scrim-harness.md`
   (run dir moves) and `docs/inventory`-style caveats in `docs/e2e-testing.md:187-190` if the deploy-log
   capture lands.

**Acceptance criteria** (all falsifiable):

- A 1-team practice scrim from a fresh worktree, then `destroy-competition.py`, yields
  `.automated-tests/<run-id>/` with `test.json`, `collection.json`, `RED-TEAM.md`, `BLUE-TEAM.md`,
  `REPORT.md`, `evidence/**`, and `index.json` updated at the comp level.
- **Kill the harness mid-event, then run `destroy-competition.py` alone: `RED-TEAM.md` still lands.**
  This is the headline behaviour change; everything else is bookkeeping.
- A range whose red01 is already gone produces `unrecoverable` in `collection.json` and a provenance
  stub in `RED-TEAM.md` — not silence, and not an empty file.
- Re-running destroy twice changes nothing: second run reports `skipped (already collected)` and
  `verify` shows no hash drift.
- `python3 -m pytest tests/test_secret_hygiene.py` passes with a pulled report quoting a password, and
  `git status` shows nothing new under `.automated-tests/`.
- `REPORT.md` for a run that never had blue agents says so in the Blue section rather than showing
  zeros that read like a failed defence.

---

## 8. Decisions taken (2026-10-03)

1. **Folder name: `.automated-tests/`** as proposed — self-describing, hidden like every other
   comp-dir state file, with `index.json` + `test-artifacts.py list` for discovery.
2. **Gitignored, with no publish path.** "Gitignore it, without any way to include it." Enforced by
   `test_no_test_artifact_is_tracked`, which asserts the *tracked set*, not the rule.
3. **Warn loudly and proceed — no gate.** A dead box must never block a destroy; gaps are printed,
   recorded in `collection.json`, and restated in `REPORT.md`'s caveats. `--skip-artifacts` for speed,
   `--artifacts-timeout` for a bounded pull.
4. **Blue stays on the operator host.** `BLUE-TEAM.md` is sealed, not pulled, and blue's prompt now
   asks it to write `<workdir>/REPORT.md` — that file is the source of the canonical document, so the
   "missing blue report" warning is about the agent's output rather than about a collection bug.

Two things the implementation added beyond the plan, both because the fixture run showed they were
needed: a stub distinguishes **`not-collected`** (the evidence is still out there — recoverable) from
`absent`/`unreachable`, and `collect_for_teardown` runs `scrim-report.py` when the harness never got
to it, so a rescued run still gets a machine verdict in its report.
