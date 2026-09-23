# E2E testing — running deploys without repeating failures

How to run a full pipeline test (`create-competition.py` / `run-agent-scrim.py`) so that
failures are triaged once, recovered cheaply, and never debugged twice. This doc condenses
every deploy failure from the July 2026 shakedown through the September scrim/dress runs;
`docs/known-issues.md` stays the canonical incident log, `docs/architecture.md` explains the
phases, and `docs/usage-agents.md` documents the flags.

The three habits this doc exists to enforce:

1. **Triage before touching anything** — regression or known failure class? (§1)
2. **Never redo phase 1–2 for a failure in phase 4–7** — resume, don't rebuild. (§4)
3. **Always capture the deploy log** — several post-mortems were only possible by luck. (§3, §7)

## 1. Triage first: regression or expected?

Before debugging a failed deploy, answer: *what changed since the last green run?*

1. `git log --oneline <last-green>..HEAD` and diff anything that touched a `.py`/`.tf`
   deploy-path file. Comment-only changes don't count — verify before blaming a commit.
   (Example: `af7468c` stripped all comments; AST comparison of every module showed the code
   byte-equivalent, so same-day failures were not regressions.)
2. Match the failure signature against the table below. Only failures that match **nothing**
   get a full debugging session — and get added to this table afterwards.
3. A real regression is a commit pair: introduced-by → fixed-by. Record both in the post-mortem.

| Failure class | Signature | Verdict | Status |
|---|---|---|---|
| strict × pin density | phase 5/6 `nakon deploy --strict` → `CalledProcessError` exit 1, per-step `FAILED` lines | Expected at max-vuln pin counts: one broken vulndb row aborts the whole phase. Some legacy rows had failed *silently* for months before strict made them visible | Chronic — recover with trim-then-resume (§5), fix or avoid pinning broken rows |
| timeout sizing | `subprocess.TimeoutExpired` on apt/compose in phase 4 | Was chronic (300s compose build, 180s apt, 60s compose-up all tripped on slow cold starts) | Fixed — engine budgets raised to 600s (`63b9c31`, `0c0547e`) |
| node/storage saturation | HTTP 596, authed API hangs, node reboot, stale `lock = clone` surviving reboot | Environmental — clones saturating the datastore the template lives on; concurrent external provisioners make it worse | Recurring risk — preflight pool headroom, no concurrent provisioners (§7) |
| apt package rotation | apt 404 mid-deploy (e.g. `nginx 1.24.0-2ubuntu7.4x` vanished) | Environmental, self-heals after apt-daily refresh | Known — retry later or repair post-hoc |
| cloud-init clone race | clone has no IPv4 / APIPA; ifupdown address dropped on carrier blip; resolv.conf reverted | Environmental | Auto-repaired by `ensure_cloned_network`/`_repair_box_network`; chronic time cost before that fix |
| domain-join vs ADDS race | Domain Join fails "domain does not exist" right after promotion | Timing race — dc01 still settling | Mitigated (`487b977`: skip re-promotion via ADDS artifact + live membership probes); resume phase 6 and retry |
| deploy-path regression | anything unclassifiable, correlates with a logic-touching commit | Real regression | One on record: `2662fb7` iterated dict keys → `TypeError` in `ensure_cloned_network` at phase 6; fixed `9015f06` same day |
| event-side (not deploy) | opencode cycle deaths, scoreboard `Forbidden`, red-evidence scp hangs, wrong systemd unit names | Harness/agent-side, not the deploy pipeline | Fixed + policed by `docs/rehearsal-gates.md` |

## 2. Failure history digest

What actually happened, condensed. "Clean" runs are listed because they bound what the
pipeline does *not* break on.

| Run | Date | Outcome | Dominant failure class | Recovery used |
|---|---|---|---|---|
| July shakedown (cde-test, e2e-2026-07-20) | 07-09→07-20 | 15+ pipeline bugs fixed | Everything: silent apt failures, DNS ordering, 596 under parallel clones, compose timeout | Fix + redeploy (~15 commits) |
| solo-8box-2026-08-10 | 08-10 | Clean, phase 7 | — | — |
| win-domain-2team-2026-08-13 | 08-13→14 | 4 Windows-sizing/sequencing bugs, fixed live | Clone timeout, ADDS reboot wait, missing AD vars, DSRM prompt | Fix `2f6470d` + redeploy |
| win-linux-practice | 08-14→15 | Pass; exposed strict vs non-idempotent joins | nakon v0.1.2 duplicate-step crash | `strict=False` opt-out; v0.1.3 fix |
| e2e-2026-09-03 | 09-03 | Phase 7; sshd start-limit crash loop found | Config interaction (5 `ssh-*` rows, same-second restarts) | Hotfix `ec2d746`, re-validated |
| fire-scrim-2026-09-13 | 09-13 | Clean | — | — |
| agent-scrim-2026-09-16 | 09-16 | 5 deploy attempts to reach phase 7 | TF var gap + strict + Windows row rc=1s | Per-fix + redeploy ×4 |
| agent-scrim-2026-09-17 | 09-17 | Deploy clean; event ran; scoreboard unreachable all event | Undiagnosed then (later: Quotient session semantics) | — |
| agent-scrim-2026-09-17b | 09-17→18 | 4 mid-phase-6 resumes | Join race, APIPA, OOM, vulndb DHCP drift | Resume ×4 + guest-agent repairs |
| agent-scrim-2026-09-17c | 09-18 | ~17.5 h wall (normal: 3–5 h) | Node crash / local-lvm saturation (596), join race ×2, strict ×2 | Disk moves, console unlock, `--from-phase 2` |
| e2e-2026-09-19 | 09-19→20 | Full pass + verify PASS | Timeout sizing (apt 180s, compose 60s ×2) | `--from-phase 4` ×3; budgets raised |
| scrim-dress-2026-09-20 | 09-20→21 | Max-vuln rehearsal; repeated strict aborts | strict × 16 broken rows + 1 real regression (phase-6 `TypeError`) | Trim-then-resume; regression fixed `9015f06` |

Ranked recurring modes (most frequent first): strict × pin density → cloud-init clone race →
phase-4 timeout sizing (now fixed) → domain-join/ADDS race → vulndb row bugs surfacing only
under max-vuln pins → node/storage saturation → vulndb VM IP drift → event-side harness
failures. Note the pattern in the biggest time sinks (17c's 17.5 h, scrim-dress's repeated
aborts): **environmental saturation and unvetted pin density, not pipeline code.**

## 3. What you can rely on (recovery toolbox)

- **State file + resume.** `competitions/<id>/.deploy_state.json` records `last_phase`, teams,
  and every secret. `--from-phase N` reloads it, skips confirmation, and reuses the original
  credentials (a missing secret is regenerated once and written back). Resuming without the
  state file is a hard error by design — fresh secrets while skipping destructive phases
  desyncs the range (see `docs/architecture.md`, Operational invariants).
- **Resume markers.** `.phase6-swept` (written only after a clean full nakon sweep; a fresh
  deploy unlinks it), `cloned_vms.json` (clones already present are skipped, not re-cloned),
  `.nakon-domain-<team>-adds.json` + live join probes (skips DC re-promotion on resume).
  Phase 7 sub-steps are flag-gated (`seeded` / `engine_unpaused` / `injects_created`) — but
  `unpause_engine` is not idempotent, so never resume "to be safe" past a completed phase 7.
- **Snapshots.** `tz-base` is taken on team1 boxes in phase 5; `tz-ready` on all boxes at the
  end of phase 6; clones get `tz-base` at birth. Disk-only, replace-existing, **never raises**
  (a failed snapshot is only a WARNING — verify with `list_snapshots` if you plan to rely on
  one). `redeploy-competition.py` modes: `rollback-ready` / `rollback-base` / `reconfigure` /
  `rebuild`, with a fail-fast snapshot precheck. Snapshots need a snapshot-capable datastore —
  the current `hdd` zfs pool is fine; thick LVM cannot snapshot at all.
- **Verify gate.** `verify-competition.py` (logins, services, isolation, misconfig survival,
  injects, no-default-creds) is the pass/fail gate for any deploy claim. No test suite exists;
  this plus a deployed range *is* the test.
- **Logs — nothing captures them by default.** `run-agent-scrim.py --run-dir` writes
  `<run_dir>/deploy.log`; `run-deploy.sh` tees to repo-root `deploy.log`; a bare
  `create-competition.py` run leaves only the console scrollback. Two post-mortems (17b/17c)
  were possible only because a run-dir happened to capture output. Always launch with capture
  (§7).

## 4. Per-phase failure cost map

The cost asymmetry is the whole game: phases 1–2 are the expensive, dice-rolling part
(Proxmox/Terraform/clone); phases 4–7 are mostly idempotent re-entries. A failure late in the
pipeline never justifies starting over.

| Phase fails | Typical cause | Cheapest recovery | Never do this |
|---|---|---|---|
| 1 | (it *is* the teardown) | re-run | — |
| 2 | Terraform abort; or "already exists" state mismatch | If state mismatch: the printed hint is correct — `--from-phase 1` is the only clean path. If TF died cleanly: fix cause, re-run `--from-phase 2` (17c did this mid-life after disk moves) | Don't hand-create/destroy TF-managed resources around TF — that's what creates the mismatch |
| 4 | apt/compose timeout on the engine | `--from-phase 4` — bootstrap steps are idempotent; resumed comp this way 4 times across runs | Don't rebuild boxes for an engine-side timeout |
| 5 | nakon `--strict` on team1; DNS/auth retries | Trim broken rows (§5) → `--from-phase 5`. If it was a transient (scp reset, one flaky step): re-run once before trimming | Don't `--from-phase 1` — you'd re-roll every clone/DNS/environmental dice to avoid one bad vulndb row |
| 6 | strict on team2; clone race; join race; TypeError-class regressions | Trim (§5) → `--from-phase 6`: `.phase6-swept` absent means the sweep re-runs, but existing clones are skipped and the ADDS artifact guards re-promotion. Join races: just resume and retry | Don't assume the phase-6 resume re-clones (it doesn't); don't trust it on a domain comp without checking the `.nakon-domain-*-adds.json` artifacts exist |
| 7 | seed/inject/unpause failure | Re-run `--from-phase 7` — sub-steps are individually flag-gated | Don't re-run past an already-unpaused engine |

Rule of thumb: **the resume you already have is cheaper than the redeploy you're considering.**
Every full teardown also re-rolls the environmental failure classes (clone races, apt
rotation, datastore pressure) that caused maybe half of all historical failures.

## 5. Trim-then-resume for strict failures

The dress run worked example. `generate_nakon_config` rebuilds `nakon-config.json` from
`box_vulns.json` on *every* invocation — including resumes — so trimming pins and resuming is
safe and takes effect immediately:

1. Read the `FAILED` step names from the deploy log; note which box(es) each hit.
2. Classify: transient (scp reset, one flaky apt) → re-run once before trimming. Deterministic
   (same row fails on multiple boxes, rc=1/2/127, missing payload) → row bug, trim it.
3. Remove the broken row names from `competitions/<id>/box_vulns.json` (a 20-line throwaway
   script in the run dir is fine — the dress run trimmed 16 rows / 27 entries this way). Keep
   every trim recorded in your findings/notes; each is a vulndb bug or pin mismatch.
4. Re-run with the phase where nakon ran: `--from-phase 5` (team1) or `--from-phase 6` (team2).
5. **Trim before the first team2 resume if team1 already failed on those rows** — team2 runs
   the same rows on equivalent boxes, so the same abort will happen again otherwise.

Corollary: under `--strict`, pin density is risk. A full-catalog pin run (534 pins/team)
should be expected to iterate; don't author it the night the range needs to be green.

## 6. Upgrades that would make failures cheaper (spec — not yet implemented)

1. **Per-phase snapshot checkpoints.** `checkpoint(n)` in `deploy.py` is the single choke
   point called exactly once per completed phase — the natural hook. Spec: after saving state,
   take `tz-phase<N>` snapshots (disk-only, replace-existing, non-fatal WARNING — same
   semantics as `range_ops.take_snapshot`) on the boxes that exist at that point (team1 from
   phase 2 onward, team2+ from phase 6). Payoff: a phase-5/6 failure could roll boxes back to
   a known-good state instead of re-running bootstrap into a possibly-dirty one. Rollback path:
   extend the redeploy `rollback-*` modes with a snapshot-name override (they're currently
   pinned to the `tz-base`/`tz-ready` constants). Caveats: zfs snapshots are cheap on `hdd`,
   but rollback requires stop/start, snapshots are per-VM (no atomic group), and the scoring
   **engine (vmid 1000) is never snapshotted** — engine recovery is rebuild-only today.
2. **Always-on deploy logging.** Default the deploy to tee stdout/stderr to
   `<comp_dir>/deploy-<timestamp>.log` (the comp dir already holds 0600 secret files, so a log
   there leaks nothing new). Until
   then, §7's launch commands include the capture explicitly. This is the cheapest fix on this
   list and retroactively enables every post-mortem.
3. **Preflight pin lint.** `nakon catalog check --box-vulns` exists but isn't a gate. Run it
   before every deploy (and wire it into `run-agent-scrim.py` pre-deploy), plus lint against a
   maintained known-broken-rows list so the same vulndb row doesn't abort two runs in a row.
4. **Datastore headroom check.** The worst incident on record (17c node crash) was local-lvm
   saturation from clones landing on the template's pool. Preflight: check the target pool's
   free space ≥ (number of Windows clones + margin) × 60 GB before phase 2, and confirm
   templates/disks live on the intended pool.

## 7. Step-by-step: an e2e test run

**Preflight** (each item preempts a known failure class; total cost: minutes):

- [ ] Working tree has no deploy-path changes you can't attribute; note the current commit so
      the next triage has a `<last-green>` baseline.
- [ ] `boxes.json` templates exist on the node and none are in the known-broken list — the
      deploy only *warns* about broken templates (`debian13-lite`, `ubuntu24.04`).
- [ ] `nakon catalog check --box-vulns` passes; no rows from a known-broken list are pinned.
- [ ] Target datastore has headroom for all clones (esp. Windows, ~60 GB each).
- [ ] Nothing else is cloning on the node (workshop portal provisioner included).
- [ ] `create-competition.py --competition <id> --plan-only` and actually read the plan.
- [ ] Choose capture: harness run dir, or explicit tee (below).

**Launch** (from the repo root — state/resume paths resolve from cwd):

```bash
# full harness (deploy + verify + agents + evidence + teardown):
.venv/bin/python run-agent-scrim.py --competition <id> --new --teams 2 ...

# deploy-only test with a persistent log:
python3 -u create-competition.py --competition <id> --teams 2 --yes 2>&1 | tee deploy.log
```

**On failure** — work the decision tree, don't improvise:

1. Capture the tail (you did launch with capture, right?) and the phase that aborted.
2. Triage against §1. Regression candidate? Baseline diff before anything else.
3. strict exit 1 → §5 trim-then-resume. Timeout → check if it's the known fixed class; if it's
   a new budget, raise it and record it. Environmental (596, apt 404, clone race) → wait/repair,
   then resume at the same phase.
4. Resume `--from-phase N` where N is the phase that failed (§4 cost map). Only accept a
   `--from-phase 1` when Terraform state has actually diverged.

**After:**

- Run `verify-competition.py <id>` — no deploy counts as green without it.
- Post-mortem: new failure class → add a row to §1's table and an incident to
  `docs/known-issues.md`; regression → record the introduced/fixed commit pair; event-side
  near-miss → check whether `docs/rehearsal-gates.md` needs a new gate.
- Trims and repairs: record them; every trimmed row is a vulndb bug someone should see.
- Teardown unless the next step needs the range live (`destroy-competition.py --yes`).
