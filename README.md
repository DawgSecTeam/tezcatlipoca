# Quotient + nakon range automation

Proxmox-based scoring range. `create-competition.py` is the driver: it generates Quotient's
scoring config and nakon's machine list, then runs the deploy. Companion scripts:
`verify-competition.py`, `redeploy-competition.py`, `destroy-competition.py`. Phases and
components: [docs/architecture.md](docs/architecture.md).

**A deploy mutates real Proxmox infrastructure.** Teardown goes only through
`destroy-competition.py`, which destroys only what the deploy's own ownership tags prove it
created.

## Quickstart

Prerequisites — API token, templates, SSH keypair, tooling, vulndb reachability — are in
[docs/usage-people.md](docs/usage-people.md). Estate ground truth (hosts, tokens, template
vmids, live ownership) is in the gitignored `infrastructure-status.md`: read it before any deploy.

```bash
git clone --recurse-submodules <url>      # or: git submodule update --init --recursive
git clone ../tezcatlipoca-competitions    # competition definitions; sibling checkout
cp tezcatlipoca.example.toml tezcatlipoca.toml   # real values; stays out of git
python3 create-competition.py             # interactive
python3 create-competition.py --help      # flags: docs/usage-agents.md
```

Scripted and agent-driven use: [docs/usage-agents.md](docs/usage-agents.md). Practice runs
(capacity tests, canaries, shakedowns) must run from a fresh worktree off `main` — see
[AGENTS.md](AGENTS.md).

## Dashboard

Range runs are submitted to
[DawgSecTeam/tezcatlipoca-dashboard](https://github.com/DawgSecTeam/tezcatlipoca-dashboard);
competition definitions live in
[DawgSecTeam/tezcatlipoca-competitions](https://github.com/DawgSecTeam/tezcatlipoca-competitions).
It runs every job against an engine worktree at a pinned ref; this repo stays the engine.
Direct driver runs work and are the recovery path.

## Documentation

**[docs/README.md](docs/README.md) is the full index** — every doc, grouped by what it is for.