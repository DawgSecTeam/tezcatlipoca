# Campaign summary — three parallel ranges (2026-10-15)

What a one-session campaign against the deployment pipeline found, fixed, and could not
finish. Written to be read on its own; the full evidence (per-run logs, event captures, raw
Proxmox queries, the scrim's interaction record) is in `logs/campaign/REPORT.md` and its
siblings, and live estate values are in `../infrastructure-status.md`.

## Headline

**The pipeline's concurrent-range support did not work, and the reason was that a failure was
invisible.** Three deploys submitted within two seconds of each other all raced one Proxmox
datastore lock. Proxmox reported the losers with the failure text in the task's `status` field
and no `exitstatus`, which the wait loop did not test for: one run died in 7 seconds, and the
other two polled a task that was already dead for the full 1800-second timeout while holding
their engine locks and workspaces. Two further defects then made the resulting debris
unrecoverable by the deploy that created it.

All four are fixed on `main` with regression tests:

| Defect | Fix |
|---|---|
| A failed PVE task is polled to its timeout | only `"running"` is non-terminal (`infra/client.py`) |
| A transient datastore lock kills a whole deploy | bounded clone retry that reclaims its own partial clone (`engine/engine_template_ops.py`) |
| The placement probe vetoes the deploy's own failed build | this run's template slot is its own build attempt unless it is a kept template or another competition's range (`multinode/placement_planner.py`) |
| The same hole in the authoritative gate | same rule (`infra/collisions.py`) |
| A per-VM lock 500 kills a deploy mid-phase | `proxmox_api_for` retries that one response body |
| Every teardown lost the engine's final scoreboard | a null body is a recorded error, not an `IndexError` (`quotient/event_capture.py`) |

After those fixes, three ranges were deployed concurrently, verified, and torn down cleanly,
and a fourth full-range deploy plus teardown proved template reuse and reclamation end to end.

## What was exercised

- **Deployment**: full eight phases on crimson-relay (AD + mixed Windows/Linux + portal),
  pfsense-rvb (in-path pfSense firewall, AD with Linux members), scrim-one-light (red/blue).
- **Template reuse**: re-deploying a competition after a teams-only teardown kept the engine
  template and all goldens — phase 2 dropped from 622.9 s to 50.3 s, and the reuse survived the
  move into a fresh dashboard workspace exactly as documented.
- **Placement**: node lists honoured; a run pinned to the unreachable catalog node was refused
  with a named cause; a submit naming an unknown node was refused before a run record existed.
- **Networking**: team-to-team connectivity blocked with the target provably alive and the
  source still reaching the internet (the isolation gate, PASS on all three ranges); transit
  bridges created for the firewall competition.
- **Configuration**: nakon plants and the plant-coverage gate PASS on every range (6-8
  machines each).
- **Verification**: the verifier ran before T0 and on demand; every FAIL was traced to either
  a killed deploy or an already-open known issue — none was left as "expected".
- **Teardown**: a teams-only teardown, a full teardown with template reclamation, and a scrim
  deployed range torn down; all three exit 0 and all three independently re-proved by querying
  Proxmox rather than trusting the exit code.
- **Lifecycle**: start (clock running, injects created), and the deploy-side hold/start split.

## The scrim: a failed run, honestly

Two blue agents and a deployed red01 were present, and T0 was reached — but the run produced
**zero interaction**: red achieved no footholds (the assume-breach seed reported all six boxes
unreachable and then exited 0, which the harness believes — an existing open issue, now seen a
third time), and blue's one completed cycle printed the inject ticket and stopped without
making a change or submitting. The event also overran its own 30-minute window and was still
running at 56 minutes because the orchestrator waits on a blue cycle that had already exceeded
its budget. Every rehearsal gate fails. **This is a red-versus-empty-room run and is recorded
as a failed run, not as a red result.**

## Still open

Filed in `known-issues.md`: per-run red01 slotting (two concurrent runs of one competition
cannot get distinct red01 vmid/IP without editing the competition definition), the
`TEZ_*`/`TF_VAR_*` env mismatch that silently breaks teardowns and bad-auto, a blue cycle
overrunning the event deadline, no reclaim path for a retired range's kept templates, and a
dashboard resolver that can target a stale workspace. The student portal remains unproven
(its deploy was killed before phase 8), and no multi-node placement was possible because the
second catalog node was unreachable all session.

## On the placement mess (the question asked)

`logs/campaign/placement-simplification.md` is the full analysis. In one line: put every
identity in a single per-run placement object written before preflight, derive red01's vmid
from the slot count instead of the fixed `+170` offset (which really does collide, at slot 2
with the tool's own limits), select management addresses from a free-address scan instead of
defaulting them, and collapse the four collision gates into one proof — while leaving run
ownership, teardown tag matching and the template hash lifecycle exactly as they are.

---

# Round 2 (later the same day) — consolidation, re-run, and what it proved

After the campaign above, every outstanding branch and worktree was consolidated onto `main`
(now the only branch, old tips kept as `pre-consolidation/*` tags), and the pipeline was run
again end to end. Full evidence: `logs/round2/FINDINGS.md`, one report per range, and
`logs/consolidation-patches/CONSOLIDATION.md`.

## Two more real defects, found by re-running

1. **The datastore-lock retry did not outlast the contention.** Round 1's fix retried three
   times 20 s apart; a three-way race still lost, failing at +0:03, +0:36 and +1:11 while the
   winners' clones committed seconds apart at the very start. What is contended is the
   serialised allocation queue behind the lock, not one long clone. Now five attempts backing
   off 40/80/120/160 s (~6.5 min), and the regression test asserts the **total wait**, not the
   attempt count.
2. **The unbooted (DC) golden conversion never waited for its `base-` volume rename.** Linked
   clones only work from `base-<vmid>-disk-N` volumes and the rename is asynchronous: the
   booted path had guarded it since 2026-10-04, the DC path never did. A run died at `[4b/8]`
   with `HTTP 500 Linked clone feature is not supported for 'hdd:vm-2350-disk-0'`, and the
   earlier failure's own `qmtemplate` task is on record as having hit a storage-lock timeout —
   its UPID was discarded, so the driver printed success while the rename never happened. Both
   paths now share one guard, and the next run proved it live: the guard polled twice (2 s
   each) and the rename landed on the third poll, with the team boxes then cloning from
   `base-2350-disk-0`.

Both fixes are on `main` with regression tests. A third and fourth fix went into the dashboard
(not this repo): its range resolver could target a workspace whose state document has no
resources — a refused run's stub outranked the live range, so range actions died on "no engine
management IP recorded" — and a top-level `comp_dir` in the request body was silently dropped.

**Provenance matters, and it bit this round:** every run deployed between the push and the
dashboard's next `fetch origin`, so all of them executed the *older* commit while reporting
`engine_ref: main`. The first line of each `run.log` (`$ engine origin/main @ <sha>`) is the
only trustworthy statement of what a run executed; "we ran main" is not evidence.

## What round 2 verified

- **pfsense-rvb** — deploy 8/8; **`firewall_in_path PASS`** (engine routes each team subnet
  `via 172.31.<id>.2`, holds no `192.168.<id>.1`, both firewall WANs answer on `:22`, transit
  bridges present) and **`domains PASS`** (both DCs serving their own forest with distinct
  DomainSIDs; the Fedora member joined, so the SELinux/guest-agent edge is not a domain-path
  failure). Full teardown, templates reclaimed, estate re-proved.
- **crimson-relay** — deploy 8/8 on the fixed code; `domains PASS`, `isolation PASS`,
  `portal PASS`; the held-start verify fails only on `misconfig` (the long-open known issue)
  and on gates that need a scored round, which the deliberate `--hold-start` withholds. Full
  teardown with zero warnings.
- **scrim-one-light** — deploy 8/8; verify's sole failure is the same known `misconfig`. The
  scrim itself is a **failed run**: the harness's own verdict is *"NO INTERACTION — blue never
  engaged"*, interaction score 1 (a single inject submission), with zero takedowns,
  restorations, evictions and eradications. Red's assume-breach seed failed on all six boxes
  and still exited 0, so the harness announced success — the same open bad-auto defect, now
  seen a fourth time. Both agents were present and live; they were starved, not absent.

## Newly found, not yet fixed

*"Not yet fixed" as of the day this campaign ended — this section is the round's own record and is
left as written. Every item below was fixed on 2026-10-16 with a regression test: see
[defect-remediation-2026-10-16.md](defect-remediation-2026-10-16.md) for the fix and its proof.*

- **No `[7/8]` banner exists** — phase 7 prints it only on the skip branch, so every deploy's
  log and the dashboard's stage jump 6 → 8.
- **`.deploy-timings.jsonl` mislabels and double-counts the AD work** — the `domain_adds` rows
  are filed under phase 6 while their parent `domains` is phase 7 and they run concurrently,
  so the same wall clock is counted twice and the per-phase totals can exceed the run's own
  duration.
- **`bad-perms-userConfig` is absent on every clone while nakon returns rc 0 and
  `plant_coverage` PASSes** — a contradiction between two gates that needs a decision, not
  another re-run.
- **No range-level mutex for `scrim`** — a second scrim of the same range is accepted; two ran
  concurrently here (each stops nginx for its fire test, and both log in as the same accounts)
  until one was cancelled.
- **An orphan reclaim leaves the range's bridges behind**, and a bridge carries no tags, so no
  destroy path can ever attribute one.
