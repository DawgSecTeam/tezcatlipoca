# Docs index

Every document in this directory, grouped by what it is for. If you are new, read
[README.md](../README.md) → [architecture.md](architecture.md) → the usage doc that matches how you
drive the pipeline.

**The pipeline is version 2** (golden templates + linked clones, phases renumbered 2026-09-24).
[architecture.md](architecture.md#v1-to-v2-migration-what-moved) maps the old phase numbers, and any
passage written against the pre-golden pipeline is a bug — see [AGENTS.md](../AGENTS.md#editing-docs).

## Evergreen — how the system works

| Doc | Read this when… |
|---|---|
| [architecture.md](architecture.md) | You need the system view: the seven phases, component map, data flow, stage split, isolation model, snapshot table, nakon contract, secrets, state files, invariants, timeout rationale |
| [internals.md](internals.md) | You are changing a module and need the "why" behind a symbol, constant, ordering, or workaround |
| [glossary.md](glossary.md) | A term is being used precisely and you are not sure which sense: team key vs identifier, slot, anchor, golden vs unbooted golden, stage vs pass vs bundle |
| [multi-node.md](multi-node.md) | You are running one competition across several Proxmox hosts (`nodes.json`, placement, jump routing, per-slot goldens, `sync-template.py`) |
| [packet-profiles.md](packet-profiles.md) | You are compiling a competition packet into a range, or authoring a `packet.yaml` |
| [rehearsal-gates.md](rehearsal-gates.md) | You need the numeric pass/fail gates for a scrim run |
| [benchmark-m02.md](benchmark-m02.md) | You are questioning a timeout, a parallelism setting, or a resource assumption — the linked-vs-full-clone v2 measurements |
| [benchmark-m04.md](benchmark-m04.md) | Same, for the per-competition template lifecycle: validation matrix, rebuild granularity, Windows/domain numbers |
| [vulndb-fixes/](vulndb-fixes/) | You are loading a **fresh** vulndb from `nakon/vulndb/seed.sql` and need the catalog rows that were patched by hand |

## Operations — running and repairing

| Doc | Read this when… |
|---|---|
| [usage-people.md](usage-people.md) | You are setting up or driving a deploy by hand — prerequisites, template building (incl. the Windows traps), the **env-var reference**, running, teardown, mid-competition recovery, troubleshooting |
| [usage-agents.md](usage-agents.md) | You are driving the pipeline non-interactively — CLI flags, pre-authored configs, verify/redeploy/destroy, operational modes |
| [e2e-testing.md](e2e-testing.md) | A deploy failed and you need to triage it, or you are planning a full pipeline test (§1 triage, §4 cost map, §5 trim-then-resume, §7 checklist, §8 multi-host/pfSense/red) |
| [known-issues.md](known-issues.md) | Something is broken, or you need a decision. **Open/pending issues only** |
| [known-issues-triage-2026-10-02.md](known-issues-triage-2026-10-02.md) | You want to know why an issue is (or is not) on the open list — the full 69-entry triage that produced the current split |
| [incident-archive.md](incident-archive.md) | A failure looks familiar — resolved incidents kept for the *why* behind each mitigation |
| [environment-facts.md](environment-facts.md) | You are about to pick a template, pool, vmid block, or node — node/storage/template ground truth with verification status |
| [security-disclosures.md](security-disclosures.md) | You touch credentials, `.env` variants, or git history — exposures, and the open token rotation |
| [upstream-defects-handoff.md](upstream-defects-handoff.md) | You own the vulndb catalog or nakon — the defects not fixable from this repo |
| [scrim-harness.md](scrim-harness.md) | You are running or modifying the red-vs-blue agent scrim (`run-agent-scrim.py`, `scrim-report.py`, `beacon_ops.py`) |
| [tests.md](tests.md) | You are running the offline suite, adding a test, or wondering what the suite does *not* cover |
| [pfsense-inpath-2026-09-28.md](pfsense-inpath-2026-09-28.md) | You are wiring an in-path pfSense firewall per team (topology + the reproducible guest-side config-injection method). Dated, but an operational runbook, not a run report |

## Reports — what actually happened

Dated run reports, post-mortems, and one-off run prompts. Read them for evidence and for the shape
of a past failure; the evergreen lessons have been folded into the docs above.

| Report | When |
|---|---|
| [reports/shakedown-5x4-2026-09-28-report.md](reports/shakedown-5x4-2026-09-28-report.md) | Full-stack readiness shakedown (5 boxes × 4 teams): what was proven green, the findings detail, event results |
| [reports/amongus-cde-2026-report.md](reports/amongus-cde-2026-report.md) | CDE "Among Us" build + 2-team validation run: spec→pipeline mapping, template recipes, live-found fixes |
| [reports/svc-matrix-2026-09-28-report.md](reports/svc-matrix-2026-09-28-report.md) | All-scored-services matrix (16 pins, Windows+Linux) |
| [reports/pfsense-rvb-2026-09-28-report.md](reports/pfsense-rvb-2026-09-28-report.md) | 2-team AD + in-path pfSense + bad-auto red, 60 minutes |
| [reports/loadtest-cyberrange-2026-09-29-report.md](reports/loadtest-cyberrange-2026-09-29-report.md) | Cyberrange capacity/load test |
| [reports/winad-testrun-2026-09-25-report.md](reports/winad-testrun-2026-09-25-report.md) | Windows-heavy AD test run |
| [reports/regression-4x1-2026-09-28-report.md](reports/regression-4x1-2026-09-28-report.md) | 4×1 regression run (same-TYPE pin collapse, golden disk resize) |
| [reports/same-type-2box-2026-09-29-report.md](reports/same-type-2box-2026-09-29-report.md) | Same-type 2-box closeout (bad-auto destroy identity) |
| [reports/packet-validation-cde-2026-2026-09-30.md](reports/packet-validation-cde-2026-2026-09-30.md) | The first packet-compiled deploy — the nine pipeline failures it surfaced, with their shapes |
| [reports/scrim-extreme-cyberfield-2026-09-22-postmortem.md](reports/scrim-extreme-cyberfield-2026-09-22-postmortem.md) | Cyberfield extreme dress-rehearsal post-mortem |
| [reports/scrim-live-2026-09-26-postmortem.md](reports/scrim-live-2026-09-26-postmortem.md) | scrim-live post-mortem |
| [reports/harness-upgrades-plan.md](reports/harness-upgrades-plan.md) | Scrim harness rev-2 upgrade record (what changed, where it landed, what is left) |
| [reports/dress-rehearsal-prompt.md](reports/dress-rehearsal-prompt.md) | The full-dress-rehearsal runbook/prompt for the first practice sweep |
| [reports/deploy-cyberfield-prompt.md](reports/deploy-cyberfield-prompt.md) | The agent prompt + Cyberfield wiring notes from the cyberfield deploy |

Entry points are [README.md](../README.md) (quickstart + project overview) and
[AGENTS.md](../AGENTS.md) (repo rules, incl. the practice-run worktree rule).
