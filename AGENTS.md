# AGENTS.md — tezcatlipoca

Guidance for agents (and humans) working in this repo.

## What this is

`tezcatlipoca` is the Proxmox-based scoring-range driver. `create-competition.py` generates
Quotient's scoring config and nakon's machine list, then runs a seven-phase deploy (terraform →
engine bootstrap → DNS/nakon on team1 → clone to other teams → seed). Companion scripts:
`destroy-competition.py`, `redeploy-competition.py`, `verify-competition.py`.

## Ecosystem position

```
vulndb-ui (catalog) ── read by ──> nakon ── submodule + CLI ──> tezcatlipoca (this repo)
                                                            │
                                                            └── builds a bundle, ships it to the
                                                                scoring engine, runs `nakon deploy` there
```

This repo consumes **nakon** as a git submodule at `vendor/nakon`, invoked **only as a CLI**
(`nakon randomize` to pick a competition's configs, `nakon build` to make a bundle, `nakon deploy`
on the scoring engine). It does **not** import nakon internals and does **not** touch the vulndb
directly (no MySQL) — all catalog access goes through nakon. It does **not** depend on vulndb-cli
(it does no catalog CRUD/attachments). Scoring is **Quotient** (cloned to the engine), not
huitzilopochtli.

## Layout

```
create-competition.py   shim re-exporting pipeline (keeps `import driver` working)
deploy.py / config_ops.py / nakon_ops.py / windows_ops.py / hardening_ops.py / clone_ops.py / domain_ops.py / engine_ops.py   pipeline (split from create-competition.py)
constants.py            vmid math, snapshots, budgets, NAKON_DIR
ssh_ops.py              gateway SSH + Terraform ctx + wait helpers
range_ops.py            Proxmox API, guest-agent exec, snapshots, enumerate_targets
utils.py                Compfile/users/competition loading
quotient/setup.py       event.conf/credlist/seed/injects setup
terraform/main.tf       bridges, scoring VM (1000), team1 VMs, NIC wiring
vendor/nakon/           SUBMODULE (v0.1.3) — CLI only (randomize/build/deploy)
docs/architecture.md    architecture (phases, data flow, isolation)
docs/                   usage-people.md, usage-agents.md
destroy-competition.py  teardown (API clones + terraform destroy)
redeploy-competition.py per-team rollback/reconfigure; verify-competition.py checks
generate-packet.py      competitor briefing packet
```

## Run / build / test

```bash
git submodule update --init --recursive     # populate vendor/nakon
cp .env.example .env                         # TF_VAR_* + quotient creds
# vendor/nakon/.env is also needed for build-time catalog access (or set VULNDB_UI_URL)
python3 create-competition.py                # interactive
python3 create-competition.py --help         # flags in docs/usage-agents.md
```

No test suite. Verify a change by running `verify-competition.py` against a deployed range, and/or
exercising the nakon CLI directly from `vendor/nakon` (`python3 -m nakon randomize --json …`).

## Conventions & gotchas

- **nakon is a CLI dependency, not a library.** `generate_nakon_config()` calls
  `nakon randomize --json` (via `_nakon_randomize`); `build_nakon_bundle()` runs `nakon build`;
  `run_nakon()` scp's the package + bundle to the engine and runs `nakon deploy`. Don't re-add an
  in-process `from nakon…` import or a direct MySQL connection — that's the coupling this was
  unwound from.
- **`vendor/nakon` is pinned to a release tag.** Bump it deliberately: `cd vendor/nakon &&
  git checkout vX.Y.Z`, then commit the new submodule pointer. Don't develop nakon inside this
  checkout — work in the nakon repo, tag a release, then pin it here.
- **`vendor/nakon/.env`** (gitignored) holds the vulndb creds for build/randomize; the bundle
  itself carries no creds to the engine.
- **`NAKON_DIR = Path("vendor/nakon")`** in `constants.py` — nakon runs with that as cwd (so it can read its `.env`) and bundles live under `vendor/nakon/bundles/` (content-addressed, shared).
- **`os_to_platform`** lives in `nakon_ops.py` (mirrors nakon's), re-exported via the shim; `redeploy` uses `driver.os_to_platform` so classification can't disagree.
- **Per-run secrets** (teams.json, event.conf, credentials.txt, nakon-config.json, .deploy_state.json)
  are gitignored; boxes.json/Compfile/box_services.json are non-secret and tracked.
- **Resumable deploy:** `--from-phase N`; pinned `box_services.json`/`box_vulns.json` make re-runs
  deterministic (same selection → same bundle cache hit).

## Integration contract

- **In:** a Compfile + boxes.json (and optional pinned box_services.json/box_vulns.json).
- **Calls nakon:** `randomize --platform --services --vulns --exclude --source auto --json`
  (→ `{services, vulns}`); `build --config <abs> --out bundles --json` (→ bundle path);
  `deploy --bundle … --config … [--only …]` (on the engine, over SSH).
- **Env:** `.env` (`TF_VAR_*`, quotient creds); `vendor/nakon/.env` (vulndb: host/user/password/
  database, VULNDB_UI_URL).
