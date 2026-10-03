# Quotient + nakon range automation

Proxmox-based scoring range. `create-competition.py` is the driver: it generates Quotient's
scoring config and nakon's machine list, then runs a seven-phase deploy (pipeline v2, built on
golden templates and linked clones). Companion scripts: `verify-competition.py`,
`redeploy-competition.py`, and `destroy-competition.py`.

The phases, components, and invariants are described in
[docs/architecture.md](docs/architecture.md).

## Web UI

A browser front end covers most day-to-day work. You can author and edit competitions, pick
catalog pins for services and misconfigs, and launch plan, deploy, and verify jobs. It edits the
same `competitions/<id>/` files and runs the same CLI scripts an operator would type, so nothing
in it is a second source of truth.

Run it with `python3 webui/server.py` and open http://127.0.0.1:8765. Setup, the screen guide,
and the keyboard map are in [webui/README.md](webui/README.md).

## Documentation

**[docs/README.md](docs/README.md) is the full index**. It groups every doc as Evergreen,
Operations, or Reports, with one line on when to read it. The most-used entry points:

| Doc | Covers |
|---|---|
| [docs/usage-people.md](docs/usage-people.md) | Setup and operation by hand: prerequisites, templates, env-var reference, running, teardown, recovery, troubleshooting |
| [docs/usage-agents.md](docs/usage-agents.md) | Non-interactive and agent operation: CLI flags, pre-authored configs, verify/redeploy/destroy, operational modes |
| [docs/architecture.md](docs/architecture.md) | The seven phases, component map, data flow, isolation, state files, invariants |
| [docs/internals.md](docs/internals.md) | Per-module design notes: the "why" behind functions, parameters, and orderings |
| [docs/packet-profiles.md](docs/packet-profiles.md) | Competition packets to ranges: profile schema, `compile-packet.py`, packet verify gates |
| [docs/multi-node.md](docs/multi-node.md) | One competition across several Proxmox hosts: `nodes.json`, placement, jump routing |
| [docs/known-issues.md](docs/known-issues.md) | Open and pending issues only. Fixed issues are deleted, not archived |
| [docs/environment-facts.md](docs/environment-facts.md) | Node, storage, template, and vmid ground truth |
| [docs/security-disclosures.md](docs/security-disclosures.md) | Credential-exposure history and the current open rotation |
| [docs/tests.md](docs/tests.md) | The offline test suite: how to run it, what it covers |
| [webui/README.md](webui/README.md) | The web UI: what each screen edits, the catalog source, the deploy panel |
| [AGENTS.md](AGENTS.md) | Repo rules for coding agents, including the practice-run worktree rule |

## Quickstart

```bash
git clone --recurse-submodules <url>    # or: git submodule update --init --recursive
cp .env.example .env                    # fill in real values; .env stays out of git
python3 create-competition.py           # interactive
python3 create-competition.py --help    # flags documented in docs/usage-agents.md
```

Setup details, the env-var reference, and troubleshooting live in
[docs/usage-people.md](docs/usage-people.md). Scripted and agent-driven use lives in
[docs/usage-agents.md](docs/usage-agents.md).

nakon is vendored at `vendor/nakon` (currently v0.1.7) and needs `vendor/nakon/.env` for
build-time catalog access. Never commit `competitions/*/` secrets or either `.env`; the full
secret inventory is in [docs/architecture.md](docs/architecture.md#secrets).

The offline test suite lives in `tests/` and runs with `python3 -m pytest tests/ -q`. It is a
regression suite over the deploy-path helpers, not a full pipeline suite; see
[docs/tests.md](docs/tests.md). The real integration gate is `verify-competition.py` against a
deployed range.
