# Docs index

Grouped by what each doc is for. New here: [README.md](../README.md) → [architecture.md](architecture.md) → the usage doc matching how you drive the pipeline.

## How the system works

| Doc | Read when… |
|---|---|
| [architecture.md](architecture.md) | System view: phases, component map, data flow, isolation, snapshots, nakon contract, secrets, state files, invariants |
| [internals.md](internals.md) | Changing a module; the "why" behind a symbol, constant, ordering, workaround |
| [glossary.md](glossary.md) | Disambiguating a term: team key vs identifier, slot, anchor, golden vs unbooted golden, stage vs pass vs bundle |
| [multi-node.md](multi-node.md) | One competition across several Proxmox hosts |
| [packet-profiles.md](packet-profiles.md) | Compiling a competition packet into a range, or authoring `packet.yaml` |
| [rehearsal-gates.md](rehearsal-gates.md) | Numeric pass/fail gates for a scrim run |

## Running and repairing

| Doc | Read when… |
|---|---|
| [usage-people.md](usage-people.md) | Setting up or driving a deploy by hand: prerequisites, templates, env-var reference, teardown, recovery, troubleshooting |
| [usage-agents.md](usage-agents.md) | Driving the pipeline non-interactively: flags, pre-authored configs, verify/redeploy/destroy |
| [e2e-testing.md](e2e-testing.md) | Triaging a failed deploy or planning a full pipeline test |
| [known-issues.md](known-issues.md) | Something is broken, or a decision is needed. Open/pending only — fixed issues are deleted |
| [environment-facts.md](environment-facts.md) | Picking a template, pool, vmid block, or node |
| [../webui/README.md](../webui/README.md) | Authoring or deploying from the browser |
| [portal.md](portal.md) | Browser console access for blue teams |
| [scrim-harness.md](scrim-harness.md) | Running or modifying the red-vs-blue agent scrim |
| [automated-test-artifacts.md](automated-test-artifacts.md) | Finding a run's reports/evidence, or changing what teardown collects |
| [tests.md](tests.md) | Running the offline suite, adding a test |
| [remote-access.md](remote-access.md) | Headscale participant access |

Entry points: [README.md](../README.md) (quickstart) and [AGENTS.md](../AGENTS.md) (repo rules, incl. the practice-run worktree rule).
