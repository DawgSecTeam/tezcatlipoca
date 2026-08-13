# Usage — agents

Non-interactive interface: CLI flags, pre-authored config files, and machine-readable
validation, for driving this from a script or an agent instead of answering prompts by hand.
One-time Proxmox/template setup is still a human prerequisite — see
[usage-people.md](usage-people.md#prerequisites-one-time-per-proxmox-host) for that. For what
the project is and how it's architected, see the [README](../README.md).

## `create-competition.py` flags

With no flags it runs fully interactively. Flags let it skip prompts individually — you don't
need all of them, only enough to cover what you'd otherwise be asked:

| Flag | Effect |
|---|---|
| `--competition NAME` | Deploys it straight away if it already has a `Compfile` + `boxes.json`; otherwise creates it (needs `--scenario`/`--difficulty` or falls back to prompting for them). |
| `--teams N` | Number of teams — skips the "How many teams?" prompt. |
| `--yes` | Skips the confirm-deploy prompt. |
| `--scenario TEXT` | Scenario description (only used when creating a new competition). |
| `--difficulty N` | Difficulty 1–10 (only used when creating a new competition). |
| `--from-phase N` | Resume from this phase; `N > 1` skips the destructive cleanup + `terraform apply`. See the resume hint a failed deploy prints. |
| `--plan-only` | Collect/generate the competition's config and print a summary, then exit **without touching any infrastructure** — no teardown, no `terraform apply`. |

**Box selection has no flag equivalent** — naming/templating/sizing each box type
(`collect_boxes()`) is always interactive, even under `--yes`. To skip it non-interactively,
pre-author `competitions/<id>/Compfile` + `boxes.json` yourself (see
[Pre-authoring a competition](#pre-authoring-a-competition) below) and pass just
`--competition <id>`; the tool detects both files exist and deploys straight away, prompting
only for team count (add `--teams N` to remove that prompt too).

Run `python3 create-competition.py --help` for the exact current list.

### Review before a real deploy

Even with `--yes`, or with piped/scripted stdin that happens to satisfy every remaining prompt,
nothing stops the driver from walking straight into a real `terraform apply`. Use
`--plan-only` first to collect/generate the config and print the box list without touching
infrastructure, review it, then re-run the same command without `--plan-only` (plus
`--teams N --yes`) to actually deploy.

### Resuming (`--from-phase`)

`deploy()` runs seven phases (destroy-prior-range, `terraform apply`, copy SSH key, bootstrap
engine, nakon-on-team1, clone+nakon-on-other-teams, seed/start — see the
[README](../README.md#how-it-works) for what each does). A failed deploy prints which phase it
died in; `--from-phase N` re-enters there instead of tearing everything down and rebuilding
from scratch. Phase 6's clone step reads the nakon bundle by content hash, so a resume there
reuses the already-built bundle instead of rebuilding it. Phase 7's three sub-steps (seed
teams, unpause the engine, create injects) are each gated on their own flag in
`competitions/<id>/.deploy_state.json` (`seeded`/`engine_unpaused`/`injects_created`) — a
`--from-phase 7` resume after a partial phase-7 failure skips whatever already succeeded
rather than re-running it, since re-unpausing an already-unpaused engine isn't safe (see
`quotient/setup.py`'s `unpause_engine()` docstring) and re-creating injects would duplicate
them. If phase 7 needs to be forced to redo a step anyway, edit those flags out of
`.deploy_state.json` first.

## Pre-authoring a competition

To create a competition with zero interactive prompts, write these two files yourself instead
of letting `create-competition.py` generate them:

```
competitions/<id>/Compfile     # name, scenario, difficulty
competitions/<id>/boxes.json   # box list: name/template/cpu/memory_mb/disk_gb/last_octet
```

See `competitions/example/` for the exact shape. Then:

```bash
python3 create-competition.py --competition <id> --teams N --yes
```

## Pinning a competition's configuration

Randomising which services/misconfigs land on each box is the default. To choose the set
yourself instead, write either (or both) of these, keyed by box **type** (not per team — every
team defends the identical set for Quotient's wildcard-IP checks):

```
competitions/<id>/box_services.json   {"web01": ["apache"]}          scoreable services
competitions/<id>/box_vulns.json      {"web01": ["suid-find", ...]}  planted misconfigs
```

Either file on its own is enough to count as pinned; if neither exists, the run randomises
and then writes both, so a re-run of the same competition reproduces it exactly.

nakon exposes the catalog and a validator for building these programmatically:

```bash
python3 -m nakon catalog list --json
python3 -m nakon catalog check --box-vulns competitions/<id>/box_vulns.json
```

`catalog check` catches typos, building blocks requested directly instead of the misconfig
that wraps them, and platform mismatches before a deploy. See `docs/agent-selection.md` in the
nakon repo for the full catalog format.

## `verify-competition.py`

Post-deploy smoke test — reproduces the manual checks run by hand after a deploy:

```bash
python3 verify-competition.py competitions/<id> [--engine-ip IP] [--admin-password PW] [--strict-services]
```

1. **LOGIN** — every team account and admin can `POST /api/login` (HTTP 200).
2. **SERVICES** — each team's services report UP in the latest scored round (informational
   unless `--strict-services`).
3. **ISOLATION** — the team-to-team DROP rule (`range-firewall.sh`) is present in the engine's
   `FORWARD` chain, and — with 2+ teams — an actual connection from one team's box to
   another's is confirmed blocked while that same box can still reach the internet (rule
   presence alone can't tell a correct rule from one that's shadowed or misordered).
4. **MISCONFIG** — at least one planted misconfig is present on a target box (SSH via the
   scoring-engine gateway).
5. **INJECTS** — if the competition ships an `injects/` dir, the engine has that many injects.

Two additional lines: a `no_default_creds` regression guard (part of the exit-code gate,
same as logins/isolation/misconfig/injects) confirming `credentials.txt`'s box-login/credlist
lines aren't the old fixed literals, and a purely informational report of
`range-healthcheck.timer`'s current status + any recent failures it logged (see
`install_range_healthcheck()` in `create-competition.py`) — that one doesn't affect the exit
code, it's just visibility.

Exit code is `0` only when logins all pass, no default creds remain, isolation holds, the
misconfig spot-check confirms, and injects (if any) are present; service DOWN is reported but
not fatal unless `--strict-services`. Isolation is **not** demoted to informational the way
services are — a failed isolation check means teams can reach each other right now.

Data sources (all read at runtime): scoring-engine IP from `terraform output -json`
(override with `--engine-ip`), team creds from `competitions/<id>/teams.json`, admin password
from `competitions/<id>/credentials.txt` (override with `--admin-password`), SSH key/user from
`.env`, planted configs from `competitions/<id>/nakon-config.json`.

## `destroy-competition.py`

```bash
python3 destroy-competition.py
```

Destroys the team2+ boxes cloned via the Proxmox API (reads
`competitions/<id>/cloned_vms.json` — they're not in Terraform state, so `terraform destroy`
alone can't remove them), then runs `terraform destroy`. Requires that competition's
`teams.json` + `boxes.json` (both written by `deploy()`). Doesn't touch templates.

## `run-deploy.sh`

A minimal example wrapper around `create-competition.py`'s old stdin-driven interface, logging
to a gitignored file — not a documented interface in its own right. Prefer the flags above
directly; adapt the wrapper's `printf`/logging pattern only if you specifically need
stdin-driven prompt answers instead.
