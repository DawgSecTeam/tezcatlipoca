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
