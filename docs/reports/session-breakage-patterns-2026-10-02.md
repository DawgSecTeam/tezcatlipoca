# Cross-session breakage patterns (2026-10-02)

A study of every recorded agent session that touched this estate, to find the failure modes that
**recur** — and to sort them into the ones a rule or a guard can **avoid** and the ones the tooling
has to be **built around**.

This is a synthesis document, not an incident log. Per-incident detail stays in
[known-issues.md](../known-issues.md) and [reports/](.); this file exists to name the *classes*
those incidents keep falling into.

**Evidence tiers used throughout** — trust the tag, not the prose:

| Tag | Meaning |
|---|---|
| **[V]** | Verified directly against the raw source during this pass (count/quote reproduced). |
| **[R]** | Reported by a mining pass, internally consistent, **not** independently re-verified here. Treat as a lead. |
| **[D]** | Documented in this repo's own docs; cited by file. |

Re-verification recipes are in the appendix so any number here can be re-derived.

---

## TL;DR — the eight patterns

| # | Pattern | Cost signal | Verdict |
|---|---|---|---|
| 1 | **Silent success** — every layer reports OK while the artifact is dead | The single most-cited class in the docs **[D]**; `install_package` ignoring exit status, `|| true` hiding a fatal sshd error, gates failing *open*, a 26-minute fake red run producing a realistic report | Avoid — assert the downstream effect, not the call's return code |
| 2 | **Write-ahead state markers** — a marker is written before the work succeeds, so a resume silently skips it | `.nakon-domain-*-adds.json`, `injects_created`, `.postclone-swept` **[D]** | Avoid — markers must commit on success |
| 3 | **Whole-phase replay** — no per-unit checkpoint, so one bad unit re-runs the entire phase | 33 golden rollbacks (11 per golden, 3 goldens) across 10 runs; 8 consecutive failed phase-4 attempts in one day **[V]** | Avoid — per-unit markers; bounded repair loops |
| 4 | **Shared mutable state across concurrent sessions** | 5–9 concurrently active top-level sessions in **27** clock-hours; 4 worktrees + 4 sibling repos on one estate; 30 "file modified since read"; a name-prefix sweep destroyed 10+ foreign VMs **[V]/[D]** | Both — isolate by rule *and* stamp identity in code |
| 5 | **Single-path guest transports** (guest agent / SSH) | Guest-agent waits escalating 600 s → 1200 s → 2700 s → 2900 s; `guest-exec` capped at 120 s (5 logs) and *disabled* on hardened templates (3 logs) **[V]** | Build around — make `nohup`+poll the default and always keep two transports |
| 6 | **Provider/harness transport failures** | 231 model-request failures (161 network, 45 rate-limited); ~7.1 h of wall clock in failed network requests; turn failure **26 % on 09-29 and 47 % on 09-30**; 19/363 subagent records produced nothing **[V]** | Build around — idempotent, re-runnable delegations |
| 7 | **Tool-call wall clock vs hour-scale work** | Bash p99 590 s, max 2960 s; clusters at the 120 s and 600 s caps; agents add their own `timeout` and kill their own deploy **[V]** | Build around — background + log + poll, kill by PID |
| 8 | **Docs and session memory treated as ground truth** | 18 of 31 session plans open by *correcting* the previous session's notes **[R]**; a live doc contradiction found below **[V]** | Avoid — verify inbound claims, not just outbound |

---

## Corpus

| Source | Size | Range | Notes |
|---|---|---|---|
| `~/.zcode/cli/log/*.jsonl` | 132,257 lines | 2026-09-26 → 10-02 | Harness telemetry: every tool call, model request, turn, retry **[V]** |
| `~/.zcode/cli/agents/sess_*/agent_*/` | 363 subagent records | 2026-07-15 → 10-02 | `metadata.json` + output; 341 Explore, 11 general-purpose, 10 judge **[R]** |
| `~/.zcode/cli/exec/` | 794 `call_*-stdout.log` (19.5 MB) | Jul–Oct | Command stdout. **No exit codes are stored anywhere** **[R]** |
| `~/.zcode/cli/artifacts/` | 6,276 JSON, 247 MB | — | Pre-change snapshots: 5,312 `Edit` + 964 `Write` over **1,168 distinct files** **[V]** |
| `~/.zcode/cli/memories/projects/tezcatlipoca-*/` | 56 files, 2,173 lines | 2026-08-14 → 10-01 | The agents' own distilled notes **[R]** |
| `.zcode/plans/*.md` | 31 files | — | Session plans **[V]** |
| `~/cde-*.log`, repo `logs/`, `competitions/*/` | ~9 MB | 2026-07-09 → 10-01 | Real deploy runs **[R]** |

Totals for the harness log: 130,078 info / 1,920 warn / 260 error; 10,308 `tool.call.started` vs
10,198 completed and **109 failed** (≈1.1 %); 244 turns started, 192 completed, **51 failed**
(28 failed + 23 cancelled). **[V]**

Five repos share the same Proxmox estate — this checkout was the most-edited, but
`huitzilopochtli`, `bad-auto`, `workshop-vm-distribution` and `slop/slate` all appear in the
artifacts, plus three sibling worktrees of this repo. **[V]**

---

## Part A — Patterns that can be **avoided**

### A1. Silent success

Everything that reports health while the artifact is dead. This is the most expensive class
because it converts a fast failure into a late, misattributed one — often discovered by an operator
hours later.

Documented instances **[D]**: nakon's `install_package` ignores apt's exit status and the deploy
never raises; `systemctl reload … || true` hid a fatal `Match Group` error that killed SSH on every
Linux box; Docker wipes the team-subnet MASQUERADE rule and nothing notices; a box that skips DNS
repair installs nothing while Terraform exits 0; the `pins_registered` gate computed an *empty*
expected set and therefore **failed open**; `team_nics`' remote-exec "claimed success" while the
guest had neither netplan nor NICs.

Observed live in sessions **[R]**: `WARNING: DNS fix failed for … after 8 attempts — proceeding
anyway` (×3); `deploy_domain_configs` swallowing `run_concurrent` results; beacon "liveness"
implemented as `sleep 1 && pgrep`, so a process that dies at second 2 counts as planted.

**Avoid.** Two rules carry most of the weight:

1. **Assert the downstream effect, not the call's OK.** "Did apache answer on :80" — not "did the
   install command return".
2. **Gates fail closed.** An empty expected set is a FAIL, not a SKIP. Tri-state gates already exist
   (`verify-competition.py`) **[D]**; the `pins_registered` regression is what happens when one
   isn't.

### A2. Write-ahead state markers

A marker is written *before* the work it records, so a failed step is skipped forever on resume.

Documented **[D]**: `.nakon-domain-<team>-adds.json` written before ADDS promotion runs and never
cleaned on failure → "a failed first promotion is silently skipped forever"; `injects_created` set
even when `create_injects` swallowed per-inject failures; a fresh deploy inheriting a previous
deploy's `.postclone-swept`; `.postclone-swept` not invalidated when phase 4 re-cloned the boxes;
stale phase-7 flags claiming "seeded" against a fresh DB.

The same shape appears outside the repo **[R]**: a stale `TF_VAR_template_vm_id` in a hand-named
env variant (9106 → 9088 → 955/1007 across variants), a stale `placement.json`, stale TOFU host
keys, stale `/run/network/ifstatenew` on a node making *every* `ifreload -a` fail.

**Avoid.**

- Markers are written **after** success, and carry a fingerprint of the input they attest to.
- **Couple invalidation to the action that invalidates it** — the code that re-clones phase-4 boxes
  must be the code that unlinks `.postclone-swept` (this was fixed for `.postclone-swept` on
  2026-09-29 **[D]**; the pattern is general).
- Read progress from `.deploy_state.json`, never from the deploy log **[R]**.

### A3. Whole-phase replay (no per-unit checkpoint)

One bad unit re-runs everything upstream of it, so the cost of a failure scales with the size of the
phase rather than the size of the fault.

Strongest single measurement in this study **[V]**: `Golden re-entry: rolling golden-<name> back to
'tz-base' before re-planting…` appears **33 times — 11 each for `web01`, `ftp01`, `db01` — across 10
separate runs**. The failing box was always the same one (`ftp01`/`base-windows-server`); the other
two goldens were rolled back and re-planted 11 times each for nothing.

The consequence is visible in the deploy history: `cde-2026` burned eleven attempts
(`deploy6` → `deploy16`) across 2026-09-29/30, eight of them consecutive failures on phase 4
(21 × `Unable to connect to port 22 on 192.168.120.242` in `deploy11/12/13`; sysprep first-boot
timeouts in `deploy14/15/16`, `retry`, `retry2` — **4 × 1800 s and 2 × 900 s**). **[V]**

Note that raising the budget **did not fix it**: the timeout was raised 900 s → 1800 s and still
tripped. The docs already name the real remedy — "under contention, wait the load out before
rebuilding Windows goldens rather than looping" **[D]**.

**Avoid.**

- Per-golden completion markers (hash + converted template id): on a phase-4 resume, rebuild only
  the slots whose marker is missing; keep the all-goldens rollback behind an explicit
  `--rebuild-goldens`.
- **Bound the loop in code, not in discipline.** The standing rule is "max 2 repair-resume cycles;
  a third consecutive resume is not a repair, it's a resume-loop" **[D]** — good rule, and the
  11-attempt storm shows a rule alone is not enough. A counter that refuses the third consecutive
  resume of the *same phase with the same failure signature* is the enforceable version.
- Gate on **node load** rather than sleeping: the operators hand-wrote `load=32.06 (attempt 1/36)
  … load below 10 — launching phase-4 resume` **[R]**. That belongs in the pipeline.

### A4. Tool-surface waste

Pure overhead, no diagnostic value. Across the harness logs **[V]**:

| Failure | Count |
|---|---|
| `File has not been read yet. Read it first before writing to it.` | 40 |
| `File has been modified since read … Read it again` | 30 |
| `String to replace not found in file.` | 11 |
| `Bash was cancelled and the child process was asked to stop` | 13 |
| `Found 2 matches … replace_all is false` | 2 |
| `File does not exist. Note: your current working directory is …` | 2 |
| `Write was cancelled before the file operation completed` | 1 |

Plus **36 `tool.permission.denied`, every one with `ruleId: mode.plan.nonReadOnly`** — agents in plan
mode repeatedly attempting shell commands **[V]**.

**Avoid.** Read-before-edit; prefer small anchored edits over large literal blocks on
fast-moving files; don't author `Bash` calls in plan mode. Note the edit contention (`deploy.py` is
the second-most-edited file on this machine, 144 times **[V]**) — the "modified since read" failures
are a symptom of many sessions editing one hot file, so A4 partially folds into B1.

### A5. Secret hygiene by enumeration

Three separate disclosures **[D]**, all from rules that enumerated names instead of matching shapes:

- `.gitignore` listed `".env"` and `".env.realm-backup*"` **by name** and missed a hand-named
  variant — a live Proxmox token, the real endpoint, and per-team passwords reached the public
  remote. The token is permanently public until rotated.
- `git add -A` swept a `.env.realm-backup` into a commit **[R]**.
- Comp-dir terraform artifacts (`tfvars.json`, tfstate with `team_passwords`) were tracked because
  the gitignore patterns were root-anchored and never covered comp dirs **[D]**.

**Avoid.** It is already solved, and the solution is the generalizable part: a **blanket pattern**
(`.env.*` with `!.env.example`) plus a **test** (`tests/test_secret_hygiene.py`) rather than a
comment. The docs state the principle — "rule-by-rule gitignore edits silently reopen this class,
which is why the guard is a test and not a comment" **[D]**. Any new "list the bad names" rule
anywhere in this repo should be treated as a future disclosure.

### A6. Docs as ground truth

**18 of 31 session plans contain language correcting reality** — several open with a literal
`## Corrections found by code verification` section **[R]**. Examples: the plan corpus records that
the old known-issues claim about Quotient keying per `(box, type)` "was wrong; entry rewritten";
that a claimed nakon fix "does not exist anywhere — both checkouts are at the same commit"; that a
"modified docs" `git status` was stale.

One contradiction is live right now **[V]**: `docs/e2e-testing.md:46` says the Fedora per-box fixes
should "Fold into the template/catalog (still a TODO)" while `docs/known-issues.md:516` says "the
old 'fold into the golden/catalog' TODO is retired."

**Avoid.** `AGENTS.md` already mandates verifying claims against source when *writing* docs. The
recurrence shows the rule is not applied to **inbound** claims — memory notes and previous plans are
consumed as fact. A cheap convention: any session note that states a code fact carries the commit
or file:line it was verified at, and is treated as stale if the file has changed since.

---

## Part B — Patterns that must be **built around**

### B1. Shared mutable state across concurrent sessions

This is the structural one, and it is deliberate (parallel sessions are a workstyle, not an
accident).

Measured **[V]**:

- **27 clock-hours with ≥5 top-level sessions actively working, peak 9** (2026-09-26 → 10-01).
  Counting any session id that emitted a turn, a phase, a tool call or a subagent spawn.
- **4 git worktrees** of this repo on disk (`main`, `-loadtest-cr`, `-scale8`, `-testcomp`) plus
  **four sibling repos** (`huitzilopochtli`, `bad-auto`, `workshop-vm-distribution`, `slop/slate`)
  all appearing in the same session corpus. They share one flat vmid space and one Proxmox estate.
- **18 stale engine-lock files** left across two nodes.
- **30** `File has been modified since read` failures — concurrent writers on hot files.

Documented consequences **[D]/[R]**: two sessions running the identical task on the same
branch/worktree, reaping each other's deploys and then one teardown destroying the other's engines
and goldens; two comps' engines on one management IP so `clean_engine_for_template` cleaned a
**live foreign engine**; two sessions allocating ad-hoc template vmids in the same block within
minutes; a name-prefix sweep destroying 10 VMs from parallel sessions.

**Build around.** The `AGENTS.md` new-worktree rule and the tag-scoped `destroy-competition.py`
address the two worst vectors and should stay non-negotiable **[D]**. What is still missing is
**identity**:

- Every VM carries the full tag set *and* a description naming its owning comp (the clone API takes
  no tags, which is why the description exists) **[D]**. Extend the same stamp to host-side config
  (`bad-auto/config.yaml` already carries a `.deploy-stamp.json` cross-check after a stale entry
  pointed destroy at a recycled vmid **[D]**).
- Preflight should assert no *other* comp's automation is mid-flight before taking a vmid block —
  this is a coordination problem automation can partly solve and discipline demonstrably has not.

### B2. Single-path guest transports

The guest agent is the only channel before the network exists, and it is structurally unreliable.

Measured **[V]**:

- Guest-agent waits escalate within a single session: **600 s → 1200 s → 2700 s → 2900 s** for the
  same VM, which is the signature of retrying a thing that is not coming back.
- `guest-exec` has a **120 s** cap that long plants keep tripping — 5 exec logs carry
  `did not finish within 120 s`, and the harness's own error text already prescribes the fix
  (`nohup … &` and poll the log).
- `guest-exec` is **disabled outright** on hardened templates (3 exec logs:
  `The command guest-exec has been disabled for this`).

Documented **[D]**: SELinux-enforcing templates cannot be repaired through the agent at all
(`sed -i` on `/etc/ssh` denied even as root), so Fedora goldens cannot be built on such nodes;
privesc via agent is impossible in those guests.

**Build around.** The transport is the environment; the pipeline has to be dual-path and
effect-asserted:

- `nohup` + poll-by-log should be the **default** long-running guest operation, not a hint in an
  error string.
- Every scored/verify path needs **two transports** (agent + gateway SSH) and must report *which*
  one failed — "agent down" and "agent up but sshd dead" are different faults with different fixes
  (the 21 × port-22 failures were a box that probed reachable, then lost sshd **[V]**).
- Prefer a **static** management IP so a reboot cannot move the target **[D]**.

### B3. Provider transport failures and non-resumable delegation

Not the pipeline's fault and not fixable here, but it sets the budget everything else runs inside.

Measured **[V]** across 2026-09-26 → 10-02:

| Failure | Count |
|---|---|
| Model request failures | **231** |
| ├ `network_error` | 161 — generic 79, `ECONNRESET` 41, DNS `EAI_AGAIN` 37, other 4 |
| ├ `rate_limited` | 45 (incl. user/model concurrency limits, quota) |
| ├ `timeout` | 13 |
| └ `cancelled` / `invalid_request` / `unknown` | 6 / 3 / 3 |
| Model SDK stream failures (`TypeError: terminated`) | 87 |
| Retry backoffs scheduled | 231 |

Wall clock lost: **≈427 minutes** inside failed model network requests, plus 69 min in failed tool
calls and 10 min of retry backoff **[V]**.

Turn completion by day exposes the cliff **[V]**:

| Day | turns completed | turns failed | failure rate |
|---|---|---|---|
| 09-26 | 10 | 1 | 9 % |
| 09-27 | 13 | 2 | 13 % |
| 09-28 | 25 | 1 | 4 % |
| **09-29** | **64** | **23** | **26 %** |
| **09-30** | **24** | **21** | **47 %** |
| 10-01 | 52 | 2 | 4 % |

The two bad days are exactly the live-event window (`cde-2026`). Delegation fails the same way:
**19 of 363 subagent records produced nothing** (18 × `Turn execution failed`, 1 cancelled), and one
session re-dispatched the *identical* task description four times, losing all four **[V]**.

**Build around.**

- Subagent work must be **idempotent and re-runnable**, and the prompt should name the artifact path
  it is expected to produce, so a retry can detect partial work instead of re-doing it **[R]**.
- Assume the provider can remove ~half a day's throughput at the worst possible moment: schedule
  nothing irreversible (a live event, a freeze) inside the failure window, and keep the
  operator-only fallbacks separate from the LLM path (the `--blue-watchdog` pattern — a non-LLM loop
  that keeps scored units up — is the right shape **[D]**).

### B4. Tool-call wall clock versus hour-scale operations

Deploys take hours; tool calls do not. Measured **[V]**: Bash duration median **266 ms**, p90
**81 s**, p99 **590 s**, max **2960 s**, with visible clusters at the 120 s and 600 s caps.

The observed human/agent compensation is worse than the problem: agents wrap deploys in their own
`timeout` (`timeout 1500 …`, `timeout 590 …`) which converts a slow success into an abrupt
partial-state kill — **6 exec sessions carry a leading `Terminated`** **[V]**. Separately, harness
background tasks get reaped when the session ends, so a backgrounded hour-scale deploy also dies
mid-flight **[R]**.

**Build around.** One shape works and the session notes keep re-deriving it:

- `setsid nohup … > log 2>&1 < /dev/null` from the worktree root, then **poll the log**; never rely
  on a foreground tool call or the harness's background tracking for hour-scale work **[R]**.
- **Kill by PID.** `pkill`/`pgrep -f` patterns match the invoking `bash -c` and self-kill — hit in
  5+ session notes, "happened twice" **[R]**.

### B5. Hardware and capacity are the ceiling

The docs are explicit that the biggest time sinks are "environmental saturation and unvetted pin
density, not pipeline code" **[D]**. Concretely: one node hard-downs spontaneously and needs a
physical power cycle; the shared pool hit literal 0 free and capped co-tenancy at ~4–6 teams;
disk binds before RAM; the engine's round loop does not auto-resume after a reboot **[D]**.

The transferable lesson is a **liveness rule**, not a capacity number: after a site outage, ICMP
"worked" hours before any TCP did — the bridge answered for itself while forwarding nothing.
**Ping is not a liveness signal; probe the API port** (any HTTP code, not `000`) **[D]**.

---

## Part C — If you change only five things

1. **Make markers commit-on-success and input-fingerprinted** (A2). Cheapest fix with the widest
   blast radius — it is the root of the "failed step skipped forever" family.
2. **Add per-golden completion markers** (A3). Directly removes the 11× multiplied retry measured
   above, and the 11-attempt deploy storm that motivated it.
3. **Bound the repair loop in code, and gate the retry on node load** (A3). Replace
   "max 2 repair cycles" as a rule with a counter that refuses the third consecutive resume of the
   same phase with the same failure signature.
4. **Give every long guest operation a `nohup`+poll path and two transports** (B2), and make every
   verify/scored path say *which* transport failed.
5. **Turn "list the bad names" rules into shape-matching guards with tests** (A5), starting with any
   remaining enumerated ignore/allow list.

---

## Appendix — evidence and confidence

### Numbers reproduced in this pass **[V]**

`~/.zcode/cli/log` totals and level/event counts; per-day turn/tool/model success-failure table;
tool-failure message breakdown; Bash duration percentiles; concurrency (27 clock-hours ≥5 active);
permission-denial rule id; 242 `session.model_selection.persist_failed` (`FOREIGN KEY constraint
failed`) and 73 `Session not found` / `persisted_missing` — the harness's own persistence bugs, worth
knowing but not actionable here; 363 subagent records with 19 non-completions; 6,276 artifact
snapshots over 1,168 files; 33 golden rollbacks; 21 port-22 failures; sysprep timeout counts; 4
worktrees; 18 lock files; 564 tests; 247 commits.

### Leads taken from mining passes, **not** re-verified here **[R]**

The specific deploy-log line numbers in the cde-2026 runs; the exact ad-hoc sweep destruction list;
the `.250` tailnet claim; "two sessions ran the identical task"; the 23,660-line log flood; the
`ssh_via_gateway() got an unexpected keyword argument 'user'` fix history; the cross-session
collision narratives in the memory corpus. Each is plausible and some are corroborated by
`known-issues.md` **[D]**, but re-derive before quoting a number.

### Known measurement limits

- **No exit codes are recorded anywhere** (`call_*-stdout.log` is stdout only, 7 stderr files
  total). All log-derived failure counts are **text-signature based**, which over-counts (benign
  matches) and under-counts (silent failures). Treat them as ratios, not censuses.
- Only 108 of 553 exec sessions have call logs; 199 of 794 call logs are empty.
- Agent and exec session-id spaces only partially overlap, so "N sessions" is not directly
  comparable between the two.
- The 19 failed subagent records are a **floor**: a subagent that was cancelled but still returned
  some text counts as completed.

### Open contradictions found (worth resolving — none fixed here)

1. **`10.0.0.250`**: `constants.py:16` sets `DEFAULT_ENGINE_MGMT_IP = "10.0.0.250"` and
   `docs/internals.md:31` / `docs/usage-people.md:291` document it as the static default **[V]**,
   while the session-memory corpus says `.250` is poisoned on the tailnet path and must never be
   used for cyberfield engines **[R]**. Nothing in `known-issues.md` records the tailnet ban. Either
   the default or the memory is wrong.
2. **Fedora TODO**: `docs/e2e-testing.md:46` says "still a TODO"; `docs/known-issues.md:516` says
   retired **[V]**.
3. **Deleted `clone_ops` auto-repair** is still narrated in three docs as if live, alongside notes
   that the module was deleted — deliberate history, but a reader trap **[R]/[D]**.
4. **Push policy** flip-flops across memory files ("push after changes" vs "never push without an
   explicit request") **[R]**.
5. **Leaked `root@pam!agent` token** is byte-identical to the live `.env` and still authenticates;
   rotation was pending as of the last triage **[R]**. This is a security item, not a pattern item.

---

# Remediation plan

Proposals, not commitments. Every one is anchored to a named function or file so it can be checked
before it is believed. Effort is S (hours) / M (a day or two) / L (a week+). "Existing mechanism"
means the pattern is already in the tree and the work is to extend it, not invent it.

## Order of work

| Order | Item | Why first | Effort |
|---|---|---|---|
| 1 | A2 — content-addressed markers | Root of the "failed step skipped forever" family; the reference implementation already exists | S–M |
| 2 | A3b — bound the repair loop + load gate | Stops an 11-attempt storm without touching golden semantics | S |
| 3 | A3a — per-golden plant+smoke checkpoint | Removes the 11× multiplied retry measured above | M |
| 4 | B2 — detached guest exec + honest transport errors | Unblocks every long plant and every verify path | M |
| 5 | A1 — review the 33 "proceeding anyway" decision points | Bounded list, highest silent-failure payoff | M |
| 6 | B1 — cross-comp presence preflight + block lock | The last unguarded concurrency vector | M |
| 7 | A6 — fix the live doc contradictions; add ref-checking | Cheap, and it is the tax every session pays | S |
| 8 | B4 — one detached-run helper | Removes the self-inflicted `timeout` kill class | S |
| 9 | A5 — rotate the leaked token; codify the shape-not-name rule | Security, and it is nearly free | S |
| 10 | B5 — API-port liveness; automise round-loop recovery | Resilience after the known hardware fault | S–M |
| 11 | A4 — editing/dispatch discipline | Real but low severity; mostly outside this repo | S |

---

## A1. Silent success

**Do.** Two separate actions, because there are two different populations:

1. **The decision points (33 sites).** `grep -n "proceeding\|continuing anyway"` finds the places
   where a failure is *converted* into an undetected degradation — e.g. the DNS-fix warning, the
   root-disk "expansion failed (size unmeasurable) — continuing", "build VM never fully settled —
   proceeding". Review each and force one of three outcomes: **raise**, **retry with a longer
   budget**, or **an explicit opt-in flag** (`--tolerate-<thing>`) recorded in `.deploy_state.json`.
   A warning that nobody reads is not a mitigation.
2. **The gate rule.** Any gate that computes an *expected* set must assert it is non-empty before
   comparing. The `pins_registered` fail-open is the worked example and is already fixed; make the
   rule explicit in `verify-competition.py`'s shared gate helper so the next gate inherits it.

**Do not** try to allowlist all ~200 swallow sites (36 `check=False`, 73 bare `except: pass`,
104 `|| true`). That allowlist would be unmaintainable and would train people to extend it.
Many `|| true` and `check=False` sites are legitimately best-effort.

**Existing mechanism.** `tests/test_secret_hygiene.py` already solves this exact shape (a rule
expressed as a test, not a comment); `verify-competition.py`'s tri-state gates and
`.deploy_state.json["nakon_failed_steps"]` are the planted-integrity precedent.

**Test.** One test per converted decision point asserting the failure now raises or is recorded.

## A2. Write-ahead markers

**Do.** The reference implementation is already in the tree:
`nakon_ops._run_single_nakon_config` (`nakon_ops.py:795-815`) writes `.pending`, runs, and only
`os.replace`s into the done-marker on success, with a comment explaining why. Generalize it:

- Add `config_ops.commit_marker(comp_dir, name, payload)` / `config_ops.marker_ok(comp_dir, name,
  fingerprint)` implementing write-pending → run → atomic rename → 0600, and make the marker carry a
  **fingerprint of the inputs it attests to**.
- Audit and migrate the remaining markers: `.postclone-swept` (an empty file today — give it the
  clone generation/hash so a phase-4 re-clone invalidates it *by construction* rather than by a
  separate `unlink` that someone must remember), the phase-7 `seeded` / `injects_created` /
  `engine_unpaused` flags in `.deploy_state.json`, and the `.nakon-domain-*-ad-misconfigs.json` /
  `-ad-accounts.json` markers (`domain_ops.py:128,163,187`).
- Keep the existing rule and make it mechanical: **the code that invalidates a marker is the code
  that performs the invalidating action.**

**Existing mechanism.** The `.pending` pattern above; `config_ops.write_state` (atomic, 0600) as the
writer; `deploy.py:317-322` already drops stale `.nakon-domain-*.json` on a fresh deploy.

**Test.** `tests/test_marker_semantics.py`: failure mid-run leaves no done-marker; changed input
invalidates; the invalidating action removes it.

## A3. Whole-phase replay

Split into three parts; the first two are the ones I would actually land.

**A3a — per-golden checkpoint (M).** In `golden_ops.build_golden_set`, `rolls` is currently
*every* planted golden holding a `tz-base` snapshot (`golden_ops.py:510-513`), so a re-entry
re-plants the whole set. Add a per-golden `planted + smoke_passed` entry to the existing durable
record `.template-hashes.json` (`template_ops.load_template_hashes` / `save_template_hashes`,
keyed by the golden hash already computed for `golden_hashes`) and exclude checkpointed goldens from
`rolls`, the plant, and the smoke loop. Conversion still requires the whole set — see "do not" below.

**A3b — bound the loop and gate on load (S).** Two changes:

- A consecutive-failure counter keyed on **(phase, failure signature)** in `.deploy_state.json`.
  Refuse the third consecutive resume of the same phase with the same signature and print the
  destroy-and-redeploy path. This turns the standing "max 2 repair cycles" rule **[D]** into
  something the pipeline enforces.
- A pre-retry load gate. The operators hand-wrote `load=32.06 (attempt 1/36) … load below 10 —
  launching phase-4 resume` **[R]**; that belongs in `deploy.py` as a `--min-load-free` /
  wait-for-trough behaviour, because raising the sysprep timeout 900 s → 1800 s demonstrably did not
  fix the underlying contention **[V]**.

**A3c — build Windows goldens outside the parallel pool (M).** Both Windows failure shapes
(sshd reachable then dead; sysprep first boot never completing) were the dominant phase-4 killer,
and `build_golden_set` boots Windows goldens through a `max_workers=4` pool
(`golden_ops.py:529`). A `windows_golden_serial` option would trade wall clock for a much lower
variance on a contended host.

**Do not convert goldens one at a time as each passes smoke.** That is tempting and wrong: the boot
smoke is a deliberate phase barrier — `tests/test_parallel_golden.py:254`
(`test_failed_boot_smoke_blocks_every_template_conversion`) asserts that a failed smoke prevents
*every* `POST /template`, because a partially converted golden set is unrecoverable (templates
cannot be un-templated) **[D]**. A3a deliberately keeps that barrier: it skips *re-work*, not the
barrier.

**Existing mechanism.** `golden_hashes` + `.template-hashes.json` + `stored_template_hash` are
already the golden identity; `tests/test_parallel_golden.py` already pins the ordering.

**Test.** Extend `tests/test_parallel_golden.py`: a checkpointed golden is not rolled back or
re-planted; a *failed* smoke still blocks every conversion; a changed hash invalidates the
checkpoint.

## A4. Tool-surface waste

**Do.** Low severity, high frequency — treat as discipline, not architecture:

- A short **editing discipline** block in `AGENTS.md`: read before editing; prefer small anchored
  edits over large literal blocks on hot files; do not author mutating shell commands while in plan
  mode (36 denials, all `mode.plan.nonReadOnly` **[V]**).
- The `File has been modified since read` half (30 **[V]**) is not an editing problem at all — it is
  B1 (many sessions, one hot file). Fixing A4 alone will not move it.

**Do not** expect a repo change to fix this class; the leverage is in the harness (auto-read before
edit) and in task framing.

## A5. Secret hygiene

**Do.** The mechanism is solved and is the point: shape-matching plus a test
(`.gitignore`'s blanket `.env.*`, `tests/test_secret_hygiene.py`) **[D]**. Two actions:

- **Rotate the leaked `root@pam!agent` token.** It is a security item, independent of any pattern
  work, and it is still live **[R]**.
- Adopt the general rule: **any new enumerated list — ignore, allow, deny, or skip — is a future
  disclosure.** If a rule names things, pair it with a shape match and a test.

## A6. Docs as ground truth

**Do.**

- Fix the two live contradictions now: `docs/e2e-testing.md:46` ("still a TODO") vs
  `docs/known-issues.md:516` ("retired") **[V]**, and resolve the `.250` question (below).
- Change the convention to cover **inbound** claims: a session note or plan that asserts a code fact
  should carry `file:line@commit`. `AGENTS.md` already mandates verification when *writing* docs;
  the recurring cost is in *reading* stale ones.
- Add `tools/check-doc-refs.py` — fails when a doc cites a file whose content changed since the
  recorded commit. Prefer **symbol** references over line numbers: the memory corpus reports that
  every `~:NNNN` reference died in the comment purge **[R]**.
- Extend the existing "doc invariant as a test" pattern. `tests/test_scrim_defects.py:765` already
  asserts the retired `.phase6-swept` name is absent from the source; the same trick works for
  retired phrases in docs.

**The `.250` question specifically.** `constants.py:16` sets `DEFAULT_ENGINE_MGMT_IP = "10.0.0.250"`
and `docs/internals.md:31` documents it **[V]**; the memory corpus says `.250` is poisoned on the
tailnet path **[R]**. Decide with evidence — probe `.250` from the tailnet path — then either change
the default or record the ban in `known-issues.md`. Do not leave both standing.

## B1. Shared mutable state

**Do.** The existing controls work and should stay: the new-worktree rule, the tag-scoped resumable
`destroy-competition.py`, the ownership guard `destroy_vm_if_exists(expect_tags=…)`, the clone-marker
description for untagged interrupted clones, and `preflight_gates`' own-leftover reclamation
(`config_ops.py:112+`). The gap is that preflight reasons only about **this** comp's vmids. Add:

- **Cross-comp presence preflight.** Enumerate all VMs tagged `tezcatlipoca`, group by the `comp-*`
  tag, and if another comp's range looks mid-deploy (its engine-template build or goldens exist and
  are recent), warn — or refuse without `--concurrent-ok`. Today two sessions can take the same
  13xx golden block within minutes **[D]**.
- **Advisory block lock.** `~/.tezcatlipoca/locks/` already holds `engine-<node>-<vmid>.lock`
  (18 stale files found **[V]**). Add `block-<node>-<firstvmid>` taken for the duration of a deploy,
  and have preflight refuse a block held by a live process. Include a stale-lock reclaim path, the
  way destroy already has one.
- **Audit identity coverage on every creation path.** The foreign-engine incident names
  `clean_engine_for_template` as the weak point: it acts on an IP, not a VM identity
  **[D]**. Make it resolve the target by VM description/tag and refuse an ambiguous match.

**Do not** try to solve this with discipline alone. The measured baseline is 27 clock-hours at ≥5
concurrent sessions, peaking at 9 **[V]**; rules have already been written for this and it still
happened.

## B2. Single-path guest transports

**Do.**

- **Add a detached exec helper** in `range_ops.py`: `guest_agent_exec_detached(node, vmid, script,
  log_path, timeout, shell)` that launches `nohup … > log 2>&1 &` and polls the log — this is what
  the existing error message already tells callers to do. Migrate the long-running callers:
  `hardening_ops.py:708,771`, `redeploy-competition.py:254`, `windows_ops.py:105`, `golden_ops.py:792`.
- **Make timeouts diagnosable.** `guest_agent_exec_root` / `_windows`
  (`range_ops.py:209-248`) currently `raise RuntimeError("… didn't finish within {timeout}s")`.
  On expiry, fetch `exec-status` plus the tail of the log/output and put both in the error. Today the
  operator gets a timeout with no evidence.
- **Stop escalating the ping.** `wait_for_guest_agent` waits were observed escalating
  600 → 1200 → 2700 → 2900 s for the same VM **[V]**. Cap the total and fail with a diagnosis (VM
  status, OS type, last boot) — a VM that has not answered a ping in 45 minutes is not going to.
- **Exercise the second transport.** The `ssh_via_gateway() got an unexpected keyword argument
  'user'` failure is the lesson: a fallback that is never invoked on the happy path silently rots.
  Add the fallback drill to `docs/rehearsal-gates.md`'s checklist (a drill already exists for the
  verify path — extend it to the deploy path).
- **SELinux.** Ship Fedora templates with `/etc/selinux/config` permissive, as
  `known-issues.md` already recommends **[D]** — it is a template-build change, not a pipeline one.

**Existing mechanism.** `diagnose_unreachable_box` (`range_ops.py:171`) is the shape to reuse for
diagnosed failures; the two-transport ladder exists in verify and now needs to be as good in deploy.

**Test.** Unit tests for the detached helper (log polling, exit-code extraction, timeout diagnosis)
and for "both transports failed → error names each".

## B3. Provider transport failures

**Do.** Nothing here fixes the provider. Four mitigations that reduce what a lost turn costs:

1. **Fine-grained resumability** (A2 + A3a). The value of those items is not tidiness — it is that a
   47 %-turn-failure day costs minutes instead of a whole phase.
2. **Keep irreversible steps off the LLM path.** Freeze, live T0, and any teardown of a
   single-copy artifact must be operator-invoked per the runbook, not delegated to a session that can
   lose its transport mid-turn. The `deploy-cyberfield-prompt.md` / `dress-rehearsal-prompt.md` style
   is the right shape during an event; the agent is an accelerator for build and validate.
3. **Non-LLM watchdogs for anything that must stay up.** `run-agent-scrim.py --blue-watchdog` is the
   proven pattern **[D]** — a plain loop, no model dependency.
4. **Idempotent delegation.** Subagent prompts should name the artifact they are expected to produce,
   so a retry can detect partial work; forbid re-dispatching an identical task description (one
   session lost the same task four times **[V]**).

**Watch it.** The per-hour provider failure rate is a legitimate scheduling input: do not start a
multi-hour deploy when the last hour looks like 09-29/09-30.

## B4. Tool-call wall clock

**Do.** One helper, used everywhere a command can outlive a tool call:

- `setsid nohup <cmd> > <comp>/logs/<run>-<ts>.log 2>&1 < /dev/null`, print the log path and PID,
  then poll. This belongs in `utils.py` (next to `run_terraform`, which already gets the
  process-group semantics right) and in `run-deploy.sh`.
- **Kill by PID.** Never `pkill -f`/`pgrep -f` with a pattern that also appears in the invoking
  command line — the notes report self-kills, "happened twice" **[R]**.
- Print a `resume with: create-competition.py … --from-phase N` line at each phase boundary so a
  killed run is self-describing.

**Existing mechanism.** `utils.run_terraform` (`utils.py:201+`) already runs in its own process group
and forwards SIGINT with a SIGKILL grace — the incident that motivated it is recorded in its
docstring. Extend the same discipline to non-terraform long ops.

## B5. Hardware and capacity

**Do.**

- **Make the API port the only liveness signal** used by preflight, verify and any helper: `curl -k
  https://<node>:8006/` and accept any HTTP code but `000`. ICMP answered for hours while nothing
  was forwarded **[D]**.
- **Automate post-hard-down recovery** on the resume path: the engine's round loop needs
  `POST /api/competition/start` + `/api/engine/pause` (or `verify --fix-round-loop`) after the node
  comes back **[D]**. Wire that into resume rather than leaving it to memory.
- **Keep the headroom gates**, and fix the known disk-tax flow gap: goldens inherit the base
  template's storage rather than `TF_VAR_datastore`, which is what makes the shared pool the binding
  constraint at ~4–6 teams **[R]**. That is a placement bug with a capacity consequence, not a
  tuning problem.

---

## What I would *not* do

- **Not convert goldens individually** (breaks the deliberate smoke barrier +
  `tests/test_parallel_golden.py:254`).
- **Not raise timeouts further.** 900 s → 1800 s was tried and did not help **[V]**; the problem was
  contention, so the answer is a load gate plus per-unit checkpoints.
- **Not blanket-fail on every `WARNING`** (177 in the source). Most are genuinely advisory; the
  review target is the 33 that convert a failure into an undetected one.
- **Not build a 200-entry swallow allowlist.** It would rot and would train people to extend it
  instead of fixing the site.
- **Not treat docs as the authority for a change.** Per `AGENTS.md`, if a doc and the code disagree
  and the code looks like the bug, record the discrepancy rather than silently following the doc.
