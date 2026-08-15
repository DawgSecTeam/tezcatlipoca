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
create-competition.py   the driver: 7-phase deploy + generate_nakon_config/build/run_nakon,
                        + deploy_domain_configs() (AD forests per team — Windows members via
                        Add-Computer, Linux members via realmd/sssd; roles in domain_roles.json)
destroy-competition.py  full teardown (incl. API-cloned team2+ boxes not in Terraform state)
redeploy-competition.py per-team rollback/reconfigure mid-competition
verify-competition.py   post-deploy checks
range_ops.py            Proxmox API, guest-agent exec, snapshots, NAT/DNS helpers
utils.py                Compfile/users/competition loading
quotient/               Quotient event.conf/cledlist/injects setup
generate-packet.py      competitor briefing packet
vendor/nakon/           SUBMODULE (currently v0.1.2) — nakon for build/randomize/deploy
terraform/              team bridges, scoring VM, team1 boxes
docs/                   usage-people.md (interactive), usage-agents.md (non-interactive)
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
- **`NAKON_DIR = Path("vendor/nakon")`** — nakon runs with that as cwd (so it can read its `.env`)
  and bundles live under `vendor/nakon/bundles/` (content-addressed, shared across competitions).
- **`os_to_platform`** lives in `create-competition.py` (mirrors nakon's); `redeploy` uses
  `driver.os_to_platform` so classification can't disagree with what generates the config.
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
