# Quotient + nakon range automation

Proxmox-based scoring range. `create-competition.py` is the driver: it generates Quotient's
scoring config and nakon's machine list, then runs a seven-phase deploy. Terraform is one phase
of that — it builds only team1's boxes, the scoring engine, and the team bridges; the driver
then bootstraps Quotient, runs nakon, and clones team1's boxes out to the other teams over SSH.
nakon SSHes into each box to install services and deploy misconfigs.

**Usage docs**: [for people](docs/usage-people.md) (interactive setup + operation) ·
[for agents](docs/usage-agents.md) (CLI flags, pre-authored configs, non-interactive)

> **Setup:** clone with submodules (`git clone --recurse-submodules …`, or
> `git submodule update --init --recursive` in an existing checkout) — nakon is vendored at
> `vendor/nakon` (pinned to a release; currently v0.1.2). For agent/integration context see
> **[AGENTS.md](AGENTS.md)**.

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
   repairs it, snapshots each box as `tz-base` (see below), then ships nakon's config/bundle to
   the engine and runs it there against team1.
6. **Clone team1's boxes to the other teams + run nakon on them** — full-clones team1 via the
   Proxmox API (rewriting each clone's IP/bridge), records the new vmids in
   `competitions/<id>/cloned_vms.json` (**not** in Terraform state — only
   `destroy-competition.py` can remove them), then repeats DNS-fix/auth/nakon for every other
   team. Ends by snapshotting every box as `tz-ready`.
7. **Seed the competition** — logs into Quotient's API, assigns each team its subnet, starts
   the clock (each sub-step individually resumable — see `docs/usage-agents.md`), and uploads
   `injects/` if present.

At the end it writes `competitions/<id>/credentials.txt` (mode 0600) with the scoreboard URL,
admin login, every team's login, and the scoring-engine SSH command — the only place those
passwords are shown. Teardown is `destroy-competition.py`, not `terraform destroy` alone, since
the API-cloned team2+ boxes aren't in Terraform state.

See [docs/usage-people.md](docs/usage-people.md) for the interactive walkthrough of all of
this, or [docs/usage-agents.md](docs/usage-agents.md) to drive it non-interactively.

## Recovering one team's boxes mid-competition

A deploy is all-or-nothing, which is the wrong shape when a single team's box breaks an hour
into an event. So phases 5 and 6 take two cheap disk-only (no-RAM) snapshots of every box:

| Snapshot | Taken | Holds |
|---|---|---|
| `tz-base` | after the box boots with working DNS + `ubuntu` auth, before nakon | a clean box, nothing planted |
| `tz-ready` | end of phase 6, after nakon + service hardening | exactly the box the competition starts on |

`redeploy-competition.py` then puts a *filtered* set of boxes back — one team, one box type,
one team's linux boxes — without touching anything else:

```bash
python3 redeploy-competition.py --competition <id> --teams 3               # whole team
python3 redeploy-competition.py --competition <id> --teams 3 --boxes web01 # one box
python3 redeploy-competition.py --competition <id> --teams 3 --dry-run     # just look
```

Rolling back to `tz-ready` takes seconds and restores the box as delivered. Heavier modes
(`--mode rollback-base`, `reconfigure`, `rebuild`) are described in
[docs/usage-agents.md](docs/usage-agents.md#redeploy-competitionpy). Snapshots need a
snapshot-capable datastore (ZFS, LVM-thin, Ceph, or qcow2 on file storage — thick LVM can't);
a range deployed without them falls back to `reconfigure`/`rebuild`.

Note that a rollback **discards whatever the defending team did to that box** — it's a reset to
a known-good point, not a repair.

## Layout

| Path | What it is |
|---|---|
| `create-competition.py` | Entry point and seven-phase driver (above). |
| `redeploy-competition.py` | Targeted recovery — snapshot rollback / reconfigure / rebuild for a filtered set of boxes (above). |
| `destroy-competition.py` | Teardown — destroys API-cloned team2+ boxes then `terraform destroy`. |
| `verify-competition.py` | Post-deploy smoke test (logins, services, misconfig spot-check, injects). |
| `generate-packet.py` | Renders `competitions/<id>/packet.md`, a team-agnostic competitor briefing (network/services layout) — no live infra needed. |
| `range_ops.py` | Shared Proxmox API layer, vmid math, snapshots, and the `(team, box)` target list every per-box step iterates. |
| `terraform/main.tf` | Infra: team bridges, the scoring VM (vmid `1000`), team1's target VMs, and the engine's team-facing NIC wiring. Package install/Quotient/nakon were moved to Python — Terraform no longer does them. |
| `terraform/variables.tf` / `outputs.tf` | Every configurable setting (`TF_VAR_<name>`), and the JSON blob (`agent_context`) the driver reads via `terraform output -json`. |
| `terraform/scripts/prepare_boxes.py` | Legacy DNS-repair helper from when nakon ran inside `terraform apply` — dead, superseded by `fix_dns_on_boxes()` in the driver. |
| `quotient/setup.py` | Builds `event.conf`/`linux.credlist`, seeds/starts the competition via Quotient's API, uploads injects. |
| `vendor/nakon/` | **Submodule** pinned to a nakon release (currently v0.1.2). Invoked as a CLI (`nakon randomize`/`build`/`deploy`) to generate `competitions/<id>/nakon-config.json` and a content-addressed bundle; only the bundle (not vulndb credentials) is shipped to the scoring engine. Needs its own `.env` for build-time catalog access. Set up with `git submodule update --init --recursive` after clone. |
| `.env` / `.env.example` | The single config file for both Terraform (`TF_VAR_*`) and `create-competition.py`. `.env` is gitignored; `.env.example` is the committed placeholder version. |
| `competitions/<id>/` | Per-competition state: `Compfile`, `boxes.json`, `users.json` (optional — themeable usernames), `box_services.json`/`box_vulns.json`, `domain_roles.json` (optional — Windows DC/member roles; Linux member boxes are joined via realmd/sssd, see [usage-people.md](docs/usage-people.md#windows-domain-join-boxes)), `teams.json`, `cloned_vms.json`, `credentials.txt`, `nakon-config.json`, `packet.md`. |

## Secrets

Never commit `.env`, `vendor/nakon/.env`, `competitions/*/nakon-config.json`, or
`competitions/*/event.conf` — all gitignored already (`.env.example` is the committed,
placeholder version of `.env`, safe to share). For shared environments, prefer real env vars
over writing secrets to disk at all:
```bash
export TF_VAR_proxmox_api_token="..."
export TF_VAR_quotient_admin_password="..."
```

## Known limitations

- **Box/credlist usernames are themeable, not secret**: every target VM's login and the
  `admin`/`user1`/`user2`-equivalent credlist accounts default to those names but can be
  renamed per competition via `competitions/<id>/users.json` (see
  [usage-people.md](docs/usage-people.md#theming-usernames)) — the names themselves aren't
  secret either way. The *passwords* are never fixed: generated fresh per competition
  (`box_password`/`box_creds` in `deploy()`) — see `credentials.txt` after a deploy.
- **Hardcoded Quotient internals**: Postgres/Redis passwords for Quotient's own docker-compose
  network are literals written into `/opt/quotient/.env` by the driver, not sourced from
  `.env` — internal to that network, but not zero-hardcoded-secrets.
- **Windows scoring is port-open only**: Quotient has no native SMB/RDP/WinRM check type, so
  those get a generic `Tcp` check (dial-and-connect, same mechanism the Linux `telnet-service`
  config uses) — it can't tell a healthy service from one just listening. The Windows deploy path
  itself (including full domain-join — see [usage-people.md](docs/usage-people.md#windows-domain-join-boxes))
  is verified end-to-end; the one still-untested corner is nakon's `winget`/`choco`
  package-manager fallback, which no catalog config used in that pass.
