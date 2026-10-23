# Docs index

Every doc, grouped by what it is for. Entry points: [README.md](../README.md) (quickstart),
[AGENTS.md](../AGENTS.md) (repo rules, incl. the practice-run worktree rule).

## How the system works

| Doc | Read when… |
|---|---|
| [architecture.md](architecture.md) | System view: phases, component map, data flow, isolation, nakon contract, state files, invariants |
| [internals.md](internals.md) | Changing a module: the "why" behind a symbol, constant, ordering, workaround |
| [glossary.md](glossary.md) | Disambiguating a term: team key vs identifier, slot, anchor, golden vs unbooted golden, stage vs pass |
| [multi-node.md](multi-node.md) | One competition across several Proxmox hosts |
| [packet-profiles.md](packet-profiles.md) | Compiling a competition packet into a range, or authoring `packet.yaml` |
| [competition-spec.md](competition-spec.md) | Generating a new competition from a box/OS/service JSON spec |
| [rehearsal-gates.md](rehearsal-gates.md) | What a scrim's pass/fail gates mean and when they lie (the numbers live in `scrim_report/gate_table.py`) |
| [tlaloc/README.md](../tlaloc/README.md) | The red teamer itself: loop, decision layer, effects, C2, seed, reports. Deploy contract: [tlaloc/docs/deploy.md](../tlaloc/docs/deploy.md) |

## Running and repairing

| Doc | Read when… |
|---|---|
| [usage-people.md](usage-people.md) | Setting up or driving a deploy by hand: prerequisites, templates, config reference, teardown, recovery, troubleshooting |
| [usage-agents.md](usage-agents.md) | Driving the pipeline non-interactively: flags, pre-authored configs, verify/redeploy/destroy |
| `../infrastructure-status.md` (gitignored, local only) | Which hosts exist, token sources, datastores, template vmids, live ownership. **Read before any deploy** |
| [e2e-testing.md](e2e-testing.md) | Triaging a failed deploy or planning a full pipeline test |
| [known-issues.md](known-issues.md) | Something is broken or a decision is needed. Open/pending only |
| [environment-facts.md](environment-facts.md) | Host-agnostic runtime, template and vmid behavior |
| [scrim-harness.md](scrim-harness.md) | Running or modifying the red-vs-blue agent scrim |
| [automated-test-artifacts.md](automated-test-artifacts.md) | Finding a run's reports and evidence, or changing what teardown collects |
| [tests.md](tests.md) | Running the offline suite, adding a test |
| [portal.md](portal.md) | Browser console access for blue teams |
| [remote-access.md](remote-access.md) | Headscale participant access |
| [tezcatlipoca-dashboard](https://github.com/DawgSecTeam/tezcatlipoca-dashboard) | Central run queue: authoring, deploying, MCP for agents |