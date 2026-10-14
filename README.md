# Quotient + nakon range automation

Proxmox-based scoring range. `create-competition.py` is the driver: it generates Quotient's
scoring config and nakon's machine list, then runs an eight-phase deploy (pipeline v3, built on
golden templates and linked clones). Companion scripts: `verify-competition.py`,
`redeploy-competition.py`, and `destroy-competition.py`.

The phases and components are described in
[docs/architecture.md](docs/architecture.md).

## Dashboard

Competition authoring and range runs are centralized in
[DawgSecTeam/tezcatlipoca-dashboard](https://github.com/DawgSecTeam/tezcatlipoca-dashboard)
(the run queue + MCP server + web UI, deployed at `http://10.0.0.143:8787`) with definitions in
[DawgSecTeam/tezcatlipoca-competitions](https://github.com/DawgSecTeam/tezcatlipoca-competitions).
The dashboard runs every job against an engine worktree at a pinned ref via `--comp-dir`; this
repo stays the engine. Manual driver runs still work (see below) and are the recovery path.

## Documentation

**[docs/README.md](docs/README.md) is the full index**. It groups every doc as Evergreen,
Operations, or Reports, with one line on when to read it. The most-used entry points:

| Doc | Covers |
|---|---|
| [docs/usage-people.md](docs/usage-people.md) | Setup and operation by hand: prerequisites, templates, env-var reference, running, teardown, recovery, troubleshooting |
| [docs/usage-agents.md](docs/usage-agents.md) | Non-interactive and agent operation: CLI flags, pre-authored configs, verify/redeploy/destroy, operational modes |
| [docs/architecture.md](docs/architecture.md) | The eight phases, component map, data flow, isolation, state files, invariants |
| [docs/internals.md](docs/internals.md) | Per-module design notes: the "why" behind functions, parameters, and orderings |
| [docs/packet-profiles.md](docs/packet-profiles.md) | Competition packets to ranges: profile schema, `compile-packet.py`, packet verify gates |
| [docs/multi-node.md](docs/multi-node.md) | One competition across several Proxmox hosts: `nodes.json`, placement, jump routing |
| [docs/known-issues.md](docs/known-issues.md) | Open and pending issues only. Fixed issues are deleted, not archived |
| [docs/environment-facts.md](docs/environment-facts.md) | Node, storage, template, and vmid ground truth |
| [docs/tests.md](docs/tests.md) | The offline test suite: how to run it, what it covers |
| [tezcatlipoca-dashboard README](https://github.com/DawgSecTeam/tezcatlipoca-dashboard) | The central run queue: web UI, run API, MCP tools |
| [docs/portal.md](docs/portal.md) | The student portal: Quotient team login → Proxmox web console per box (Compfile `portal 1`) |
| [AGENTS.md](AGENTS.md) | Repo rules for coding agents, including the practice-run worktree rule |

## Quickstart

```bash
git clone --recurse-submodules <url>    # or: git submodule update --init --recursive
git clone ../tezcatlipoca-competitions  # competition definitions (sibling checkout)
cp .env.example .env                    # fill in real values; .env stays out of git
python3 create-competition.py           # interactive
python3 create-competition.py --help    # flags documented in docs/usage-agents.md
```

Setup details, the env-var reference, and troubleshooting live in
[docs/usage-people.md](docs/usage-people.md). Scripted and agent-driven use lives in
[docs/usage-agents.md](docs/usage-agents.md).

nakon is vendored at `vendor/nakon` (currently v0.1.7) and needs `vendor/nakon/.env` for
build-time catalog access. 
