# Implementation plan — session breakage-pattern fixes

Companion to [session-breakage-patterns-2026-10-02.md](session-breakage-patterns-2026-10-02.md)
(the analysis + remediation proposals). This file is the **execution plan**: what changes, in what
order, how each is proven, and how it is validated against live infrastructure.

Status: **implemented — see the completion record at the end.** Working document; archive or
delete when the work closes.

---

## 0. Definition of done

1. Every core workstream below is implemented with an offline test that fails without the change.
2. `python3 -m pytest tests/` is green, and the repo stays pyflakes-clean.
3. Documentation no longer describes any fixed item as open or broken, and each fixed item cites the
   test that guards it.
4. A practice deploy of the pipeline runs from **a new worktree cut off `main`** against real
   Proxmox infrastructure: `create-competition.py --plan-only` → real deploy → `verify-competition.py`
   → `destroy-competition.py` (re-run until clean exit).
5. The practice run's evidence (phase transcript, verify output, teardown exit) is recorded in this
   file and the analysis report's status column is updated.

### Interpretation of "remove references to them being broken"

`AGENTS.md` and `known-issues.md` both state the incident write-ups are **kept** because "the
mitigation's rationale is the load-bearing part". So this plan does **not** delete incident history.
It means:

- an item that is fixed **stops being listed as open/broken** and gains `FIXED <date>` + a pointer to
  its guarding test;
- stale "still a TODO" / "still open" / "prerelease" phrasing is corrected where the code has moved on;
- the *reasoning* is preserved (moved to an archive section if the file needs to get shorter).

If you meant literal deletion of the incident text, say so — it is a different (and smaller) job.

---

## 1. Ground rules (non-negotiable — `AGENTS.md`)

| Rule | Source | Applies to |
|---|---|---|
| Practice run from a **new worktree cut off `main`**, never the main tree, never another run's worktree | `AGENTS.md` "Practice runs" | Phase 7 |
| Teardown is `destroy-competition.py`, **never** an ad-hoc sweep; re-run until clean | `AGENTS.md`, live-found 2026-09-30 | Phase 7 |
| Never touch `workshop-*` / `challenge-*` VMs, VM 104, VM 200, or another comp's infrastructure | `dress-rehearsal-prompt.md`, `deploy-cyberfield-prompt.md` | Phase 7 |
| **Max 2 repair-resume cycles**; on the 3rd failure stop and report | `e2e-testing.md` §7 | Phase 7 |
| Freeze **last**, after the final commit | `known-issues.md` | Phase 7 if frozen |
| Docs verified against source; never edit code from a docs pass | `AGENTS.md` "Editing docs" | Phase 6 |
| No live secrets in git; `.env*` never committed | `tests/test_secret_hygiene.py` | all |

---

## 2. Verified starting state (2026-10-02)

| Fact | Evidence |
|---|---|
| Working tree on `main`, clean except 3 untracked files (`competitions/cde-2026/packet.md`, `docs/known-issues-triage-2026-10-02.md`, `docs/reports/session-breakage-patterns-2026-10-02.md`) | `git status --short` |
| Primary `.env` targets **10.0.0.193** (`pve`, `hdrives-zfs`) with **`TF_VAR_template_vm_id=9088`** | `.env` |
| `.env.cyberrange-20260930` targets **10.0.0.150** (`proxmox`, `hdd`) with `TF_VAR_template_vm_id=955` | `.env.cyberrange-20260930` |
| `nodes.json` absent → single-node deploy path | repo |
| Test suite baseline: **564 passed, 92 subtests passed in 14.95 s** across 49 files — green and fast enough to run after every change | `python3 -m pytest tests/ -q` |
| Leftover live ranges from 3 past comps (43 VMs, 20 running) and an **un-rotated leaked token** | `docs/known-issues-triage-2026-10-02.md` **[R]** — re-verify with read-only API calls before acting |

**Consequence:** the current `.env` cannot complete a deploy. `TF_VAR_template_vm_id=9088` does not
exist on either node, so the engine-base preflight hard-fails. **Fixing that is a prerequisite for
Phase 7** (W-13), and it is itself an instance of the stale-var trap (EN-1).

---

## 3. Workstreams

Legend — **Type**: code / docs / infra. **Live**: needs the practice run to prove.
Cross-refs: `triage:` = item in `docs/known-issues-triage-2026-10-02.md`.

### W-01 · Content-addressed markers (A2)
- **Type** code · **Live** yes (marker survives a resume)
- **Change**: add `config_ops.commit_marker(comp_dir, name, payload)` / `marker_ok(comp_dir, name,
  fingerprint)` — write `.pending` → run → atomic rename → 0600, fingerprint of the attested inputs.
  Generalise the reference implementation at `nakon_ops._run_single_nakon_config`
  (`nakon_ops.py:795-815`).
- **Migrate**: `.postclone-swept` (carry the clone generation/hash so a phase-4 re-clone invalidates
  by construction, replacing the manual `unlink` at `deploy_phases.py:148,404`); phase-7 flags
  (`seeded` / `injects_created` / `engine_unpaused`); `.nakon-domain-*-ad-misconfigs.json` /
  `-ad-accounts.json` (`domain_ops.py:128,163,187`).
- **Test**: `tests/test_marker_semantics.py` — failure mid-run leaves no done-marker; changed input
  invalidates; the invalidating action removes it.
- **Docs**: `known-issues.md` — the `.postclone-swept` / `.phase6-swept` entries and the
  "per-deploy domain artifacts survived fresh deploys" entry move to FIXED with a pointer here.

### W-02 · Bound the repair loop + load gate (A3b)
- **Type** code · **Live** yes (a deliberate mid-run kill proves the bound)
- **Change**: a consecutive-failure counter keyed on **(phase, failure signature)** persisted in
  `.deploy_state.json`; refuse the 3rd consecutive resume of the same phase+signature and print the
  destroy-and-redeploy path. Plus a pre-retry **node-load gate** so a phase-4 retry waits for a
  trough instead of looping (`--min-load-free`, default off).
- **Rationale anchor**: 11 consecutive attempts (`deploy6` → `deploy16`); the operators hand-wrote
  `load=32.06 (attempt 1/36) … load below 10`.
- **Test**: `tests/test_resume_budget.py` — 3rd identical resume refused; a different signature
  resets the counter; `--force-from-phase` overrides.
- **Docs**: `known-issues.md` "packet-profile validation" / phase-4 storm entries gain
  `FIXED <date>` + the enforcing mechanism.

### W-03 · Per-golden plant+smoke checkpoint (A3a)
- **Type** code · **Live** yes (the key measurement of Phase 7)
- **Change**: in `golden_ops.build_golden_set`, `rolls` currently takes *every* planted golden with a
  `tz-base` snapshot (`golden_ops.py:510-513`). Add a per-golden `planted + smoke_passed` entry to
  the existing durable record `.template-hashes.json` (`template_ops.load_template_hashes` /
  `save_template_hashes`), keyed by the golden hash already computed for `golden_hashes`, and exclude
  checkpointed goldens from `rolls`, the plant loop and the smoke loop.
- **Must not** convert goldens individually — the boot smoke is a deliberate phase barrier asserted by
  `tests/test_parallel_golden.py:254`. This change skips **re-work**, not the barrier.
- **Test**: extend `tests/test_parallel_golden.py` — a checkpointed golden is not rolled back or
  re-planted; a *failed* smoke still blocks every conversion; a changed hash invalidates the
  checkpoint.
- **Live proof**: kill the deploy during phase 4, resume `--from-phase 4`, and assert the log does
  **not** print `Golden re-entry: rolling …` for the goldens that had already passed.

### W-04 · Windows golden build outside the parallel pool (A3c) — *optional*
- **Type** code · **Live** yes
- **Change**: a `windows_golden_serial` Compfile flag that boots Windows goldens one at a time
  (`golden_ops.py:529` currently uses `max_workers=4`).
- **Why optional**: trades wall clock for variance. Decide after W-03's live result — if the
  checkpoint alone removes the pain, skip.

### W-05 · Detached guest exec, diagnosable timeouts, bounded agent waits (B2)
- **Type** code · **Live** yes (every golden build uses it)
- **Change**:
  1. `range_ops.guest_agent_exec_detached(node, vmid, script, log_path, timeout, shell)` —
     `nohup … > log 2>&1 &` + log polling. Migrate long callers: `hardening_ops.py:708,771`,
     `redeploy-competition.py:254`, `windows_ops.py:105`, `golden_ops.py:792`.
  2. `guest_agent_exec_root` / `_windows` (`range_ops.py:209-248`): on expiry, fetch `exec-status`
     plus the log tail and include both in the raised error, instead of a bare timeout.
  3. `wait_for_guest_agent`: cap the total wait; on failure diagnose (VM status, OS type, last boot)
     instead of escalating. Observed escalation: 600 → 1200 → 2700 → 2900 s for one VM.
  4. **Exercise the fallback**: add a "gateway-SSH fallback drill" to `docs/rehearsal-gates.md` so the
     `ssh_via_gateway() got an unexpected keyword argument 'user'` class cannot rot unnoticed.
- **Test**: unit tests for the detached helper (polling, exit-code extraction, timeout diagnosis) and
  for "both transports failed → the error names each".
- **Docs**: `known-issues.md` SELinux entry (`:682-688`) stays open but gains the template-side fix
  (**W-05b**, below).

### W-05b · Fedora SELinux permissive at template build (triage B4)
- **Type** infra/template · **Live** only if a fedora box is in the run
- **Change**: `/etc/selinux/config` → `SELINUX=permissive` in the fedora `-fix` template build recipe.
- **Docs**: `known-issues.md:682-688` and `usage-people.md` — the "keep fedora off such nodes"
  workaround is replaced by the build step.

### W-06 · Silent-failure decision points (A1)
- **Type** code · **Live** partly
- **Change**: review the **33** `"proceeding"` / `"continuing anyway"` sites (not the ~200 total
  swallow sites) and force each to **raise**, **retry with a longer budget**, or take an **explicit
  opt-in flag** recorded in `.deploy_state.json`. Add the fail-closed rule to
  `verify-competition.py`'s shared gate helper: a gate that computes an expected set asserts it is
  non-empty before comparing.
- **Do not**: allowlist every `check=False` / bare `except: pass` / `|| true` — it would rot.
- **Test**: one test per converted decision point.
- **Docs**: every entry whose root cause was "the failure was swallowed" gains `FIXED` + the guard.

### W-07 · Concurrency identity and locks (B1)
- **Type** code · **Live** yes (the presence preflight runs on every deploy)
- **Change**:
  1. **Cross-comp presence preflight** in `config_ops.preflight_gates` — enumerate cluster VMs tagged
     `tezcatlipoca`, group by the `comp-*` tag; if another comp's range looks mid-deploy, warn, and
     refuse without `--concurrent-ok`.
  2. **Advisory block lock** in `~/.tezcatlipoca/locks/` alongside the existing
     `engine-<node>-<vmid>.lock`: `block-<node>-<firstvmid>`, with a stale-lock reclaim path.
  3. **`clean_engine_for_template` identity** (triage B3): resolve the target by VM identity
     (description/tag), not by a shared static management IP; refuse an ambiguous match.
- **Test**: `tests/test_concurrent_presence.py` + extend `tests/test_multinode.py`.
- **Docs**: `known-issues.md:689-699` (wrong-machine cleanup, currently open) and `:700-705`
  (concurrent vmid race) move to FIXED once 3 lands; the operational notes stop saying "coordinate
  between sessions by hand".

### W-08 · One detached-run helper + kill discipline (B4)
- **Type** code + docs · **Live** yes
- **Change**: `utils.spawn_detached(cmd, log_path)` — `setsid nohup … > log 2>&1 < /dev/null`, prints
  log path + PID. Use it in `run-deploy.sh` and document it in `AGENTS.md`/`usage-agents.md`. Add a
  `resume with: … --from-phase N` line at each phase boundary. Add the **kill-by-PID** rule (never
  `pkill -f` with a pattern that appears in the invoking command line).
- **Test**: `tests/test_spawn_detached.py` (process survives the parent; log is written).
- **Docs**: `usage-agents.md` gains the "long ops are always detached" instruction.

### W-09 · Liveness, round-loop recovery, headroom (B5 + triage B2)
- **Type** code · **Live** yes
- **Change**:
  1. A single `probe_node_liveness()` (Proxmox API port; any HTTP code but `000`) and replace ICMP
     use in preflight/verify/helpers.
  2. Automate post-hard-down recovery on the resume path: `POST /api/competition/start` +
     `/api/engine/pause` (or `verify --fix-round-loop`) when the stale round-loop signature is seen.
     A timer/watchdog on the engine, same shape as `range-firewall.timer`, was the triage's proposal.
  3. Golden placement honours `TF_VAR_datastore` (goldens currently inherit the *base template's*
     storage), which is what makes the shared pool the binding constraint at ~4–6 teams. *(Verify
     this claim against `golden_ops` before implementing — it is **[R]** from a report.)*
- **Test**: `tests/test_node_liveness.py`; round-loop fix exercised by a short `verify --fix-round-loop`
  against the practice range.
- **Docs**: `known-issues.md` "Scoring round loop doesn't auto-resume" moves out of **Open issues**
  into FIXED with the watchdog; the cyberrange operational notes get rewritten (vmid 1000 is gone,
  hdd is 266 GiB not ~900 — per the triage).

### W-10 · Token rotation + shape-not-name rule (A5 + triage A1)
- **Type** infra + docs · **Live** no
- **Change**: rotate the leaked Proxmox `root@pam!agent` token and update every consumer
  (**this repo *and* sibling repos/tooling that share it** — `huitzilopochtli`, `bad-auto`,
  `workshop-vm-distribution`); delete the three plaintext copies on disk named by the triage. Add the
  standing rule: any new enumerated ignore/allow/deny/skip list must be a shape match + a test.
- **Blocker**: rotation is cross-repo and will break anything holding the old token — **needs your
  go-ahead and a window**.

### W-11 · Editing/dispatch discipline (A4)
- **Type** docs · **Live** no
- **Change**: a short "editing and dispatch discipline" block in `AGENTS.md`: read before edit; small
  anchored edits over large literal blocks on hot files; no mutating shell commands in plan mode;
  never re-dispatch an identical subagent task description; subagent prompts name the artifact they
  must produce. (Most of this class is harness-level and cannot be fixed in this repo.)

### W-12 · Docs consistency pass + reference checking (A6)
- **Type** docs + tooling · **Live** no
- **Change**:
  1. Fix the live contradictions: `docs/e2e-testing.md:46` ("still a TODO") vs
     `docs/known-issues.md:516` ("retired"); the deleted-`clone_ops` narration in three docs.
  2. Resolve `.250`: probe it from the tailnet path, then either change
     `constants.py:16 DEFAULT_ENGINE_MGMT_IP` or record the ban in `known-issues.md`. Both currently
     stand.
  3. `tools/check-doc-refs.py` — fail when a doc cites a file whose content changed since the
     recorded commit; prefer **symbol** references over line numbers (every `~:NNNN` ref died in the
     comment purge).
  4. Restructure `known-issues.md` along the triage's proposal: open traps on one screen, fixed
     incidents archived with rationale, node facts split out, security disclosures in their own file.
     This is what actually satisfies "no longer described as broken" at scale.
- **Test**: `tests/test_doc_refs.py` wrapping the checker.

### W-13 · Fix the primary `.env` dead vmid (triage A3) — **prerequisite for Phase 7**
- **Type** infra config (gitignored, never committed)
- **Change**: `TF_VAR_template_vm_id=9088` → the correct engine-base vmid on the target node
  (`955` for `.150`; determine for `.193`). Also reconcile `TF_VAR_scoring_vm_id=1080`, which
  collides with the live `cde-2026` engine on `.193` per the triage.
- **Why**: the engine-base preflight hard-fails today; and leaving it proves the trap is unfixed.

### W-14 · Reclaim orphaned ranges (triage A2) — **prerequisite for Phase 7**
- **Type** live infra
- **Change**: run `destroy-competition.py` for `amongus-cde-2026` (from `main`) and
  `scale8-scrim-2026-10-01` (from a worktree of branch `scale8-2026-10-01`, per
  `.tez-backups/scale8-preserve/`), re-running until clean. **Confirm `cde-2026`'s intent before
  touching it** — it may be deliberately live.
- **Why**: 20 running VMs, and it returns the vmids and datastore headroom the practice run needs.

### W-15 · Adjacent one-liners from the triage — *scope decision*
- **Type** code · cheap, same blast radius as W-06/W-12
- `KNOWN_BROKEN_CONFIGS` in `constants.py` + a generate-time error when a pin names one (triage B1) —
  closes the "prose cannot gate a pin" hole for `tftpd-hpa-anon-write`,
  `postgresql-remote-access`, `sshd-force-sftp-broken-chroot`, the 5 Windows user-policy pins,
  `unrealircd-backdoor-container`, winget/choco.
- `--freeze` refuses a dirty tree (triage B5).
- Assert the local-blue `CTX` against the configured slot limit at launch (triage B6).
- **Ask**: include now, or defer to a follow-up? W-15 item 1 is the highest-value of the three and
  interacts with W-06.

---

## 4. Sequencing

```
Phase 0  Baseline          W-13 (env) · reclaim W-14 · branch + offline suite baseline
Phase 1  Foundations       W-01 markers ──► W-02 loop bound ──► W-03 golden checkpoint
Phase 2  Transports        W-05 detached exec ──► W-05b SELinux
Phase 3  Failure surfacing W-06 decision points ──► W-15 (if in scope)
Phase 4  Concurrency       W-07 presence preflight + locks + engine-cleanup identity
Phase 5  Resilience        W-08 detached run helper ──► W-09 liveness + round loop
Phase 6  Docs              W-12 restructure + refs · W-10 rotation · W-11 discipline
Phase 7  Live validation   new worktree off main
Phase 8  Closeout          merge · archive plan · update reports · goal complete
```

Dependencies that matter:
- **W-03 needs W-01's marker helper** (reuse it, do not invent a second marker style).
- **Phase 7 needs W-13 + W-14**; it cannot start on the current `.env`.
- **W-12 needs Phases 1–5 finished**, otherwise it will mark FIXED things that are not.
- **W-06 and W-15.1 touch the same code path** — do them together or not at all.

---

## 5. Phase 7 — the live practice run

### 5.1 Parameters (recommendations; see §7 for the decisions I need)

| Parameter | Recommendation | Why |
|---|---|---|
| Node | **10.0.0.150** (`proxmox`, `hdd`) via `.env.cyberrange-20260930` | .193 is the owner-confirmed hard-down node; .150 hdd is 266 GiB free **[R]** |
| Competition | **`same-type-2box-2026-09-29`** (`web01` ubuntu + `win01` Windows, difficulty 1) | **two** golden box types → exercises W-03's per-golden checkpoint; Windows path exercises W-05; difficulty 1 → fewest pins → lowest vulndb risk |
| Teams | 1 | smallest real range |
| Identifiers | explicit, e.g. `TF_VAR_team_identifiers=130` after checking the block is free | the vmid-collision preflight exists because defaults collide |
| Engine vmid | explicit free `--scoring-vmid` | orphan at the default 1000 has been seen |
| Datastore | the node's, per its env variant; `TEZ_THIN_HEADROOM` only if the strict gate is wrong for the pool | documented knob |

Cheaper alternative if Windows is judged too risky: **`practice-fix-templates`** (2 Linux boxes) —
but it does not exercise the multi-golden checkpoint, which is the fix most in need of live proof.

### 5.2 Runbook

```bash
# 0. prerequisites
python3 create-competition.py --competition same-type-2box-2026-09-29 --scoring-vmid <free> --plan-only

# 1. worktree off main (AGENTS.md)
cd /home/hna/dev/dawgsec/tezcatlipoca
git worktree add -b patterns-fixes-live ../tezcatlipoca-patterns-live main
cd ../tezcatlipoca-patterns-live
git submodule update --init
cp ../tezcatlipoca/.env .env                       # NODE-PROPER variant, W-13 applied
cp ../tezcatlipoca/vendor/nakon/.env vendor/nakon/.env
cp ../tezcatlipoca/proxmox . && chmod 600 proxmox  # deploy resolves ../proxmox against terraform/

# 2. review, then deploy for real — always detached (W-08)
python3 create-competition.py --competition same-type-2box-2026-09-29 \
        --teams 1 --scoring-vmid <free> --yes \
        > logs/patterns-live-$(date +%Y%m%d-%H%M).log 2>&1 &

# 3. verify
python3 verify-competition.py same-type-2box-2026-09-29
```

All commands run **from the worktree root**; a linked worktree has none of the gitignored state and
every relative path resolves from there.

### 5.3 What the run must prove, per fix

| Fix | Live evidence to capture |
|---|---|
| W-01 markers | a resumed phase does not repeat work whose marker is committed; a failed step leaves no done-marker |
| W-02 loop bound | the deliberate kill→resume→resume→resume sequence is refused on the 3rd identical attempt |
| W-03 golden checkpoint | after a mid-phase-4 kill, the log does **not** print `Golden re-entry: rolling …` for the already-passed golden |
| W-05 detached exec | no `did not finish within 120 s`; long plants complete and their logs are on the box |
| W-06 decision points | a deliberately-broken prerequisite **raises** instead of printing "proceeding" |
| W-07 presence preflight | it detects the other comps' tagged VMs on the node and prints the block it will use |
| W-08 detached run | the deploy survives the driving shell; the resume hint is printed at a phase boundary |
| W-09 liveness | preflight probes the API port; the round loop is recovered without a manual POST |
| W-13 env | the engine-base preflight passes with the corrected vmid |
| W-14 reclaim | the node VM list drops by the reclaimed ranges before the run starts |

### 5.4 Fault injection (the part that actually tests the fixes)

A clean deploy proves the happy path only. Two bounded injections, both recoverable:

1. **Mid-phase-4 kill.** `SIGINT` the deploy while goldens are planting (one cycle, no more), then
   resume `--from-phase 4` and assert W-03's skip behaviour.
2. **Induced prerequisite failure.** Point one box's template at a nonexistent name in a scratch copy
   of the comp dir, confirm the pipeline **fails loudly** (W-06) instead of degrading, then restore.

Both are inside the "max 2 repair-resume cycles" budget. If injection 1 needs a third resume, stop and
report — that failure is itself the finding.

### 5.5 Teardown

```bash
python3 destroy-competition.py same-type-2box-2026-09-29   # re-run until it exits clean
```

Then confirm the node VM list is back to the post-W-14 baseline and the worktree is removed
(`git worktree remove`). Never substitute an ad-hoc sweep.

---

## 6. Verification matrix

| Workstream | Offline test | Live evidence | Docs updated |
|---|---|---|---|
| W-01 markers | `test_marker_semantics.py` | resume replay | `known-issues.md` marker entries |
| W-02 loop bound | `test_resume_budget.py` | 3rd resume refused | phase-4 storm entries |
| W-03 golden checkpoint | `test_parallel_golden.py` (+cases) | no rollback of good goldens | golden entries |
| W-05 detached exec | helper unit tests | no 120 s timeouts | SELinux + agent-fallback entries |
| W-06 decision points | one test per site | induced failure raises | swallowed-failure entries |
| W-07 concurrency | `test_concurrent_presence.py` | preflight sees other comps | `:689-705` |
| W-08 detached run | `test_spawn_detached.py` | deploy survives the shell | `usage-agents.md` |
| W-09 liveness | `test_node_liveness.py` | API-port probe; round loop recovered | round-loop + node notes |
| W-12 docs | `test_doc_refs.py` | n/a | all of the above |
| W-13/W-14 | n/a | preflight passes; VM list drops | `known-issues.md` env trap |

---

## 7. Decisions I need from you

1. **Node for the practice run** — .150 (recommended) or .193? And is `cde-2026` on .193 deliberately
   live, or may it be reclaimed?
2. **Competition for the practice run** — `same-type-2box-2026-09-29` (2 types incl. Windows,
   recommended), `practice-fix-templates` (2 Linux, safer), or `distro-matrix-2026-09-27`
   (fedora+alpine, exercises non-apt paths)?
3. **Scope** — implement W-15 (the triage's adjacent one-liners: `KNOWN_BROKEN_CONFIGS`, freeze
   dirty-tree guard, CTX assert) in this pass, or defer?
4. **Token rotation (W-10)** — the token is shared with sibling repos and tooling. Rotate now in a
   coordinated window, or leave it and track it separately?
5. **Merge strategy** — `AGENTS.md` requires the practice worktree to be cut off `main`. Do I land the
   fixes on `main` first and then practice (rule-compliant, but the live proof comes after merge), or
   cut the practice worktree off the feature branch (isolated, but a documented deviation)?
6. **"Remove references to them being broken"** — my reading in §0 (mark FIXED + keep the rationale +
   cite the guarding test) or literal deletion of the incident text?

Until 1–3 and 5 are answered I can still land Phases 1–5 offline (they need no live access); I will
start there unless you redirect.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Windows golden flakiness on a contended node (the reason for the 11-attempt storm) | W-03 + W-02 land first; the practice comp is difficulty 1 and single-team; load gate before retry |
| Reclaiming a range that is deliberately live | Confirm `cde-2026` intent; `destroy-competition.py` refuses foreign VMs, and it is re-run per comp, never swept |
| `.env` change leaks to other sessions | `.env` is gitignored and local; announce the change, and it is a one-line, reversible edit |
| Token rotation breaks sibling tooling | Do it in a window, grep all four repos for the token first |
| Docs pass marks something FIXED that then regresses | W-12 runs *after* Phases 1–5, and every FIXED claim must cite a passing test |
| The practice run itself leaves half-built state | It runs in its own worktree; teardown is the sanctioned tool; the worktree is disposable |

---

# Completion record (2026-10-02)

Implemented on branch `patterns-fixes` in a linked worktree; **750 tests pass**, `pyflakes` clean.
Everything below was verified against the tree, not against this plan.

| Plan item | What landed | Guard |
|---|---|---|
| A1 silent success | Tolerated-failure ledger (`utils.record_degradation`), wired into **14** sites — including the four a manual sweep missed and a source scan now enforces — persisted to `.deploy_state.json` and surfaced by `verify-competition.check_degradations()` | `test_degradations.py`, `test_degradation_coverage.py` |
| A2 markers | Verified the existing markers were already commit-on-success (no redundant helper written); fixed the real gap — `injects_created` was a bare boolean, so an inject added after the first phase-7 run was skipped forever | `test_deploy_phase_units.py` |
| A3 whole-phase replay | Per-golden plant+smoke checkpoint (`.template-hashes.json`), plus a resume budget that refuses the third identical resume and a `--min-load-free` gate | `test_parallel_golden.py`, `test_resume_budget.py` |
| A4 tool waste | `AGENTS.md` "Editing and dispatch discipline" | — |
| A5 secret hygiene | Shape-not-name rule documented and already enforced by `test_secret_hygiene.py`; **token rotation deferred by the user** and tracked in `security-docs` | `test_secret_hygiene.py` |
| A6 docs | Genre split completed, stale contradictions resolved, fixes archived with their guarding tests | — |
| B1 concurrency | Cross-deploy preflight gate (held-flock probe), engine-build identity stamp before the destructive template clean | `test_concurrent_deploys.py`, `test_engine_identity.py` |
| B2 transports | `guest_agent_exec_detached` + diagnosable timeouts + agent-wait diagnosis, with two real callers migrated | `test_detached_exec.py`, `test_auth_ladder.py` |
| B3 provider | Mitigations only (fine-grained resumability, non-LLM watchdogs) — correct, since the provider is not fixable here | — |
| B4 wall clock | `utils.spawn_detached` + the documented recipe | `test_spawn_detached.py` |
| B5 liveness/capacity | API-port liveness lesson at the failure point; the mgmt-IP gate made cluster-wide and no longer reads "unverifiable" as "free"; datastore under-counting documented | `test_engine_mgmt_ip.py` |
| Round loop (W-09.2) | `round_loop.py` (one definition of "stopped", shared with verify) + `tools/round_loop_guard.py`, installed by `engine_ops`, opt-in via Compfile `round_loop_guard 1` | `test_round_loop.py`, `test_round_loop_guard.py`, `test_round_loop_install.py` |
| Scoring account | A second admin (`scoring`) so automation can never evict an operator/harness session | `test_scoring_account.py` |

## Live practice run (2026-10-02)

A new worktree branched off this branch deployed `same-type-2box-2026-09-29` (ubuntu + Windows,
1 team, engine vmid 2400, team block 1500-1509) against **.150**, then verified and tore down:

- **Deploy succeeded** — all seven phases, `is live`, `last_phase: 7`, no failure streak, no plant
  failures.
- **Verify ran** — PASS on logins, no_default_creds, **services (UP)**, **pins_registered (6
  checks)**, isolation and **round_loop (advancing)**.
- **Destroy clean** — `hdd` went from **0.2 GiB free** (the run exhausted the pool) back to
  **154.5 GiB**, and no competition-tagged VMs remain.

What the run proved live, beyond the pipeline working: the concurrency gate refused and then
warned under its documented opt-out; the golden checkpoint recorded both boxes keyed by hash
(`web01 3861e319…`, `win01 c28ff2a5…`) and verify read back the same hashes; the failure-streak
recorder persisted `phase 2`; and the engine-build identity stamp verified before the destructive
template clean on a machine whose address another competition was also using.

## Known residual

- **The round-loop watchman has not been trialled against a live stopped loop.** It is implemented,
  installed behind a Compfile flag and tested offline; the honest completion step is to deploy a
  small range with `round_loop_guard 1`, stop the loop deliberately, and watch the timer heal it.
- **Token rotation** is the user's to schedule (`docs/security-disclosures.md`).
- Three `pyflakes` findings exist on `main` in another session's files
  (`destroy-competition.py`, `test_run_ownership.py`) — pre-existing, verified against `main`, left
  alone rather than edited under a concurrent writer.
