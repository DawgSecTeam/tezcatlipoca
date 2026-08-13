# Quotient + nakon range automation

Proxmox-based scoring range. `create-competition.py` is the driver: it generates Quotient's
scoring config and nakon's machine list, then runs a seven-phase deploy. Terraform is one phase
of that — it builds only team1's boxes, the scoring engine, and the team bridges; the driver
then bootstraps Quotient, runs nakon, and clones team1's boxes out to the other teams over SSH.
nakon SSHes into each box to install services and deploy misconfigs.

**Usage docs**: [for people](docs/usage-people.md) (interactive setup + operation) ·
[for agents](docs/usage-agents.md) (CLI flags, pre-authored configs, non-interactive)

## How it works

Team bridges have no uplink of their own, so the scoring engine is the only thing with a NIC on
every team's network — it's the jump host the driver tunnels through, where nakon actually
runs, and what NATs each team out to the internet. Because it sits on every team bridge it
*could* route team to team; `range-firewall.sh` on the engine is what actually keeps teams
apart, not the empty bridges.

`create-competition.py` is the single entry point. It loads `.env`, collects/generates the
competition's config (team count, box list, nakon's service/vuln selection), then runs
`deploy(comp_dir)`, which does **not** hand everything to one `terraform apply` — it runs seven
phases itself over SSH, printing a `[n/7]` banner for each:

1. **Clean up the previous deployment** — destroys each team's boxes, the scoring engine, and
   every team bridge via the Proxmox API. A deploy is therefore a full teardown-and-rebuild,
   not an in-place update.
2. **`terraform apply -parallelism=1`** — provisions *only* team1's boxes, the scoring engine,
   and the team bridges.
3. **(No-op)** — used to copy the SSH key to the engine; removed once nothing downstream still
   read the copy back (all SSH/SCP to team/scoring boxes uses the local key via Terraform's
   `agent_context`, and nakon authenticates to team boxes by password). Kept as a phase number
   only so `--from-phase` numbering stays stable across versions.
4. **Bootstrap the engine** — installs Docker + compose, clones Quotient into `/opt/quotient`,
   builds/starts its containers, pushes `event.conf` early (Quotient panics without it, and
   every container restart drops the team-subnet NAT *and* the team-to-team isolation rule),
   and installs `range-firewall.timer` to re-assert both every 30s for the life of the range.
   Also enables password auth on team1's boxes (nakon connects as `ubuntu` with a password
   generated fresh per competition, not a fixed literal).
5. **Fix DNS + run nakon on team1** — boxes boot with an empty `/etc/resolv.conf`; the driver
   repairs it, then ships nakon's config/bundle to the engine and runs it there against team1.
6. **Clone team1's boxes to the other teams + run nakon on them** — full-clones team1 via the
   Proxmox API (rewriting each clone's IP/bridge), records the new vmids in
   `competitions/<id>/cloned_vms.json` (**not** in Terraform state — only
   `destroy-competition.py` can remove them), then repeats DNS-fix/auth/nakon for every other
   team.
7. **Seed the competition** — logs into Quotient's API, assigns each team its subnet, starts
   the clock (each sub-step individually resumable — see `docs/usage-agents.md`), and uploads
   `injects/` if present.

At the end it writes `competitions/<id>/credentials.txt` (mode 0600) with the scoreboard URL,
admin login, every team's login, and the scoring-engine SSH command — the only place those
passwords are shown. Teardown is `destroy-competition.py`, not `terraform destroy` alone, since
the API-cloned team2+ boxes aren't in Terraform state.

See [docs/usage-people.md](docs/usage-people.md) for the interactive walkthrough of all of
this, or [docs/usage-agents.md](docs/usage-agents.md) to drive it non-interactively.

## Layout

| Path | What it is |
|---|---|
| `create-competition.py` | Entry point and seven-phase driver (above). |
| `destroy-competition.py` | Teardown — destroys API-cloned team2+ boxes then `terraform destroy`. |
| `verify-competition.py` | Post-deploy smoke test (logins, services, misconfig spot-check, injects). |
| `terraform/main.tf` | Infra: team bridges, the scoring VM (vmid `1000`), team1's target VMs, and the engine's team-facing NIC wiring. Package install/Quotient/nakon were moved to Python — Terraform no longer does them. |
| `terraform/variables.tf` / `outputs.tf` | Every configurable setting (`TF_VAR_<name>`), and the JSON blob (`agent_context`) the driver reads via `terraform output -json`. |
| `terraform/scripts/prepare_boxes.py` | Legacy DNS-repair helper from when nakon ran inside `terraform apply` — dead, superseded by `fix_dns_on_boxes()` in the driver. |
| `terraform/templates/scoring-init.yaml.tpl` | Unused — the scoring engine is cloned from a pre-built template instead. |
| `quotient/setup.py` | Builds `event.conf`/`linux.credlist`, seeds/starts the competition via Quotient's API, uploads injects. |
| `nakon/` | Symlink to a sibling nakon checkout. Queried in-process on your workstation to build `competitions/<id>/nakon-config.json` and a content-addressed bundle; only the bundle (not vulndb credentials) is shipped to the scoring engine. |
| `.env` / `.env.example` | The single config file for both Terraform (`TF_VAR_*`) and `create-competition.py`. `.env` is gitignored; `.env.example` is the committed placeholder version. |
| `competitions/<id>/` | Per-competition state: `Compfile`, `boxes.json`, `box_services.json`/`box_vulns.json`, `teams.json`, `cloned_vms.json`, `credentials.txt`, `nakon-config.json`. |

## Secrets

Never commit `.env`, `nakon/.env`, `competitions/*/nakon-config.json`, or
`competitions/*/event.conf` — all gitignored already (`.env.example` is the committed,
placeholder version of `.env`, safe to share). For shared environments, prefer real env vars
over writing secrets to disk at all:
```bash
export TF_VAR_proxmox_api_token="..."
export TF_VAR_quotient_admin_password="..."
```

## Known limitations

- **Fixed box username**: every target VM's login is always `ubuntu` (`main.tf`), because
  nakon connects with password auth rather than keys — the username isn't secret, so this is
  low-risk on its own. The *password* is not fixed: it's generated fresh per competition
  (`box_password` in `deploy()`) along with the `admin`/`user1`/`user2` credlist accounts
  Quotient's checks authenticate with — see `credentials.txt` after a deploy.
- **Hardcoded Quotient internals**: Postgres/Redis passwords for Quotient's own docker-compose
  network are literals written into `/opt/quotient/.env` by the driver, not sourced from
  `.env` — internal to that network, but not zero-hardcoded-secrets.
- **`terraform/templates/scoring-init.yaml.tpl` is dead code** — the scoring engine is cloned
  from a pre-built template instead (see [usage-people.md](docs/usage-people.md#adding-a-template-vm)).
