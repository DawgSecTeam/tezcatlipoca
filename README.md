# Quotient + nakon range automation

Proxmox-based scoring range. `create-competition.py` is the driver: it generates Quotient's
scoring config and nakon's machine list, then runs a seven-phase deploy (**pipeline v2** — golden
templates + linked clones). Terraform runs inside that deploy: apply #1 creates the scoring engine
as a linked clone of a per-competition engine template plus every team bridge; a golden-set build
turns one VM per box type into a template; apply #2 creates **every team's** boxes as linked clones
of those goldens. The driver then prepares the engine, drives nakon in three stage passes over SSH,
and seeds Quotient. nakon SSHes into each box to install services and deploy misconfigs.

Phases, and what changed from the pre-golden pipeline:
[architecture.md](docs/architecture.md#seven-phase-deploy) and the
[v1 → v2 migration table](docs/architecture.md#v1-to-v2-migration-what-moved).

## Documentation

**[docs/README.md](docs/README.md) is the full index** — every doc grouped as Evergreen /
Operations / Reports, one line each on when to read it. The most-used entry points:

| Doc | Covers |
|---|---|
| [docs/usage-people.md](docs/usage-people.md) | Interactive setup + operation: prerequisites, templates, env-var reference, running, teardown, recovery, troubleshooting |
| [docs/usage-agents.md](docs/usage-agents.md) | Non-interactive/agent operation: CLI flags, pre-authored configs, verify/redeploy/destroy, operational modes |
| [docs/architecture.md](docs/architecture.md) | Architecture: phases (+ the v1→v2 migration), component map, data flow, isolation, state files, invariants, timeout rationale |
| [docs/internals.md](docs/internals.md) | Per-module design notes — the "why" behind functions, parameters, and orderings |
| [docs/known-issues.md](docs/known-issues.md) | Open/live issues, fixed incidents kept for the why, known-broken templates, failure modes, limitations |
| [docs/e2e-testing.md](docs/e2e-testing.md) | Running e2e deploy tests: failure triage, per-phase recovery cost map, trim-then-resume, preflight checklist, multi-host / pfSense / red-team runs (§8) |
| [docs/packet-profiles.md](docs/packet-profiles.md) | Competition packets → ranges: profile schema, authoring walkthrough, `compile-packet.py`, packet verify gates |
| [docs/multi-node.md](docs/multi-node.md) | One competition across multiple Proxmox hosts: `nodes.json`, capacity-fill placement, the satellite jump router, slot vmid scheme, `sync-template.py` |
| [docs/glossary.md](docs/glossary.md) | Team key vs identifier vs slot, golden vs unbooted golden, stage vs pass vs bundle, managed vs unmanaged |
| [docs/tests.md](docs/tests.md) | The offline suite: how to run it, what it covers, how to add a test, what it does not cover |
| [docs/scrim-harness.md](docs/scrim-harness.md) | Red-vs-blue agent scrim harness (`run-agent-scrim.py`, `scrim-report.py`, beacons) |
| [docs/rehearsal-gates.md](docs/rehearsal-gates.md) | Numeric pass/fail gates for a scrim run |
| [docs/pfsense-inpath-2026-09-28.md](docs/pfsense-inpath-2026-09-28.md) | In-path pfSense firewall: topology and the reproducible guest-side config-injection method |
| [docs/reports/](docs/reports/) | Dated run reports, post-mortems, and one-off run prompts — incl. [shakedown-5x4](docs/reports/shakedown-5x4-2026-09-28-report.md), [svc-matrix](docs/reports/svc-matrix-2026-09-28-report.md), [amongus-cde](docs/reports/amongus-cde-2026-report.md) |
| [AGENTS.md](AGENTS.md) | Repo rules for coding agents, incl. the practice-run worktree rule |

## Quickstart

```bash
git clone --recurse-submodules <url>    # or: git submodule update --init --recursive
cp .env.example .env                    # fill in real values; .env stays out of git
python3 create-competition.py           # interactive
python3 create-competition.py --help    # flags documented in docs/usage-agents.md
```

nakon is vendored at `vendor/nakon` (pinned to a release; currently v0.1.7) and needs
`vendor/nakon/.env` for build-time catalog access (or `VULNDB_UI_URL`). Never commit
`competitions/*/` secrets or either `.env` — see
[docs/architecture.md](docs/architecture.md#secrets) for the full inventory. For shared
environments prefer real env vars over writing secrets to disk (`export TF_VAR_proxmox_api_token=…`).

`tests/` holds the offline suite — regression tests over the deploy-path helpers (sequencer and
per-phase units, verify gates, golden boot smoke, parallelism, bundle lint, non-apt-distro prep,
unmanaged box, interrupted-clone, run-terraform, repeat-run, packet compile/validation, multi-node
math, secret hygiene, …), not a full pipeline suite. `python3 -m pytest tests/ -q` runs them and
prints the current count; [docs/tests.md](docs/tests.md) maps what each file guards. The real
integration gate is `verify-competition.py` against a deployed range; you can also exercise the
nakon CLI directly from `vendor/nakon` (`python3 -m nakon randomize --json …`).
