# AGENTS.md — tezcatlipoca

`tezcatlipoca` is the Proxmox-based scoring-range driver: `create-competition.py` generates
Quotient's scoring config and nakon's machine list, then runs a seven-phase deploy. Companion
scripts: `destroy-competition.py`, `redeploy-competition.py`, `verify-competition.py`.

All guidance lives in `docs/` — start with:

- [README.md](README.md) — quickstart + full docs index
- [docs/architecture.md](docs/architecture.md) — architecture, nakon contract, operational invariants
- [docs/internals.md](docs/internals.md) — per-module design notes (the "why" behind the code)
- [docs/usage-agents.md](docs/usage-agents.md) — driving the pipeline non-interactively
- [docs/known-issues.md](docs/known-issues.md) — incidents, known-broken templates, failure modes
