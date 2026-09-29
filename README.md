# Quotient + nakon range automation

Proxmox-based scoring range. `create-competition.py` is the driver: it generates Quotient's
scoring config and nakon's machine list, then runs a seven-phase deploy. Terraform is one phase
of that — it builds only team1's boxes, the scoring engine, and the team bridges; the driver
then bootstraps Quotient, runs nakon, and clones team1's boxes out to the other teams over SSH.
nakon SSHes into each box to install services and deploy misconfigs.

## Documentation

| Doc | Covers |
|---|---|
| [docs/usage-people.md](docs/usage-people.md) | Interactive setup + operation: prerequisites, templates, `.env`, running, teardown, recovery, troubleshooting |
| [docs/usage-agents.md](docs/usage-agents.md) | Non-interactive/agent operation: CLI flags, pre-authored configs, verify/redeploy/destroy |
| [docs/architecture.md](docs/architecture.md) | Architecture: phases, component map, data flow, isolation, invariants, timeout rationale |
| [docs/internals.md](docs/internals.md) | Per-module design notes — the "why" behind functions, parameters, and orderings |
| [docs/known-issues.md](docs/known-issues.md) | Incident log, known-broken templates, failure modes, standing limitations |
| [docs/e2e-testing.md](docs/e2e-testing.md) | Running e2e deploy tests: failure triage, per-phase recovery cost map, trim-then-resume, preflight checklist, multi-host / pfSense / red-team runs (§8) |
| [docs/svc-matrix-2026-09-28-report.md](docs/svc-matrix-2026-09-28-report.md) | All-scored-services matrix comp (16 pins, win+linux): lineup, 16/16 UP verify, bad-auto 16/16 takedown validation |
| [docs/pfsense-inpath-2026-09-28.md](docs/pfsense-inpath-2026-09-28.md) | In-path pfSense firewall: topology, the reproducible guest-side config-injection method, and the dead-ends |
| [docs/scrim-harness.md](docs/scrim-harness.md) | Red-vs-blue agent scrim harness (`run-agent-scrim.py`, `scrim-report.py`, beacons) |
| [docs/rehearsal-gates.md](docs/rehearsal-gates.md) | Numeric pass/fail gates for a scrim run |
| [docs/harness-upgrades-plan.md](docs/harness-upgrades-plan.md) | Scrim harness rev-2 upgrade record |
| [docs/dress-rehearsal-prompt.md](docs/dress-rehearsal-prompt.md) | Full-dress-rehearsal runbook for the first practice sweep |
| [AGENTS.md](AGENTS.md) | Entry points for coding agents working in this repo |

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

Tests are offline unit tests over the deploy-path helpers (`python3 -m pytest tests/` — bundle
lint, non-apt-distro prep, unmanaged box, interrupted-clone, run-terraform, repeat-run), not a
full pipeline suite. The real integration gate is `verify-competition.py` against a deployed
range; you can also exercise the nakon CLI directly from `vendor/nakon`
(`python3 -m nakon randomize --json …`).
