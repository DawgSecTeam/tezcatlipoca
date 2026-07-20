# What this project actually does

You're spinning up a hacking-competition "range": one scoring server (Quotient) plus a
set of target boxes per team, all on Proxmox. `create-competition.py` is the driver that
runs the whole thing end to end. Terraform is only *one step* inside it — Terraform builds
team1's boxes, the scoring engine, and the team bridges; everything after that (installing
Docker/Quotient, running nakon, cloning team1's boxes to the other teams, wiring up NAT) is
done by the Python driver over SSH, not by Terraform. A separate tool called **nakon** (not
part of this repo — cloned from GitHub onto the scoring engine at runtime) SSHes into each
target box to break it in specific, scored ways.

Team bridges have no uplink of their own. The scoring engine is the only machine with a
network card on every team's subnet, so it triples as (a) every team's NAT gateway to the
internet, (b) the jump host the driver tunnels through to reach the otherwise-unroutable team
boxes, and (c) the machine that runs nakon against them. Because it has a NIC on every team
bridge it can also route team to team, so what actually isolates teams is firewall rules on
the engine (`range-firewall.sh`), not the bridges being empty.

## The pipeline, in order

`create-competition.py` is the single entry point — you run it, answer its prompts, and
everything below happens without further intervention. The prompts are stdin-driven today (a
thin argparse/flag-based CLI wrapper is being added separately, but the underlying flow is the
same):

1. It loads `.env` (`load_dotenv`) — this also makes every `TF_VAR_<name>` in there visible
   to the `terraform` subprocess calls later in the script, since they inherit the parent
   process's environment.
2. It asks whether to reuse an existing competition (replays its `Compfile`) or create a new
   one — name/scenario/difficulty, written to `competitions/<id>/Compfile` immediately once
   you answer those three prompts, not deferred until the rest of the run succeeds. Teams
   are **not** collected interactively: you're only asked *how many* teams there are;
   `collect_teams()` auto-generates `team1`, `team2`, ... with identifiers `101`, `102`, ...
   and a fresh random password for every team, every run — including teams that already
   existed in a previous run of the same competition (see "Add a team mid-event" caveat in
   README.md).
3. It decides which boxes this competition gets: `collect_boxes()` calls
   `list_proxmox_templates()` (a direct Proxmox API call using `TF_VAR_proxmox_endpoint`/
   `TF_VAR_proxmox_api_token`) to list VMs tagged `template`, then walks you through picking
   name/template/CPU/memory/disk/IP-octet per box interactively. The result is saved as
   `competitions/<id>/boxes.json`. Reusing an existing competition loads that file back
   instead of re-prompting — boxes are per-event by design (that's the whole point of scaling
   difficulty per box), so a competition you built once always rebuilds with the same boxes.
4. It rewrites `TF_VAR_teams` and `TF_VAR_boxes_per_team` in `.env` in place (`update_env()`),
   and writes the same team map to `competitions/<id>/teams.json` so `destroy-competition.py`
   can find and tear this competition down later. The event name comes from the `Compfile`,
   and the Quotient admin password is generated fresh in memory during the deploy (phase 4
   below) — neither is written back to `.env`.
5. It generates nakon's machine + vuln list itself: `generate_nakon_config()` loads
   `nakon/randomize_config.py` directly (no subprocess — nakon has no `__init__.py`, so it's
   imported via `importlib`), queries the vulndb's `configurations` table, picks services/vulns
   per box scaled by the difficulty you chose, and writes `nakon/config.json` (plus
   `competitions/<id>/box_services.json`, the scoreable services `push_event_conf()` later
   turns into Quotient checks). This used to be a separate manual step — it isn't anymore.
6. It runs the seven-phase `deploy()` pipeline described in the next section. That is where
   Terraform runs (as phase 2 of 7), followed by all the SSH-driven bootstrap, nakon, and
   cloning work.

At the very end `deploy()` writes `competitions/<id>/credentials.txt` (mode 0600) and prints a
summary — the scoreboard URL, the admin login, every team's login + subnet, and the scoring
engine's SSH command. That's the one place several of these values (the random team/admin
passwords in particular) are shown, though `credentials.txt` is now the authoritative on-disk
copy.

## How a deploy actually runs (7 phases)

`deploy(comp_dir)` does **not** hand everything to a single `terraform apply`. Terraform only
builds team1's boxes, the scoring engine (vmid `1000`), and the team bridges (`vmbr<identifier>`);
the Python driver does the bootstrap, nakon, cloning, and NAT work itself. Each phase prints a
`[n/7]` banner:

- **[1/7] Clean up the previous deployment.** Straight Proxmox API calls: destroy each team's
  boxes (`vm_id = 200 + identifier*10 + box_index`), destroy the scoring engine (vmid `1000`),
  and delete every team bridge. A deploy is therefore a full teardown-and-rebuild, not an
  in-place update — re-running the tool wipes the prior range first.
- **[2/7] `terraform init` + `terraform apply -parallelism=1`.** This provisions *only*
  team1's boxes, the scoring engine, and the team bridges (`main.tf` notes that Steps A/C/D —
  package install, Quotient, nakon — were "moved to Python"; Terraform's remaining orchestrate
  step just addresses the engine's team-facing NICs). `-parallelism=1` serializes the clones so
  Proxmox doesn't time out.
- **[3/7] Copy the SSH key to the engine.** `scp`s the automation key onto the scoring engine so
  it can act as the jump host for the isolated team boxes in the phases that follow.
- **[4/7] Bootstrap the engine.** `bootstrap_scoring_engine()` installs Docker + the compose
  plugin, clones Quotient into `/opt/quotient`, and builds + starts its containers. Then the
  driver pushes `event.conf` **early** (`push_event_conf()`, before nakon) and re-asserts NAT
  (`ensure_nat_forwarding()`). event.conf goes up first on purpose: Quotient panics on every
  scoring round while it's missing, and each container restart re-syncs iptables and drops the
  team-subnet MASQUERADE that nakon's `apt-get` needs. `push_event_conf()` also writes
  `linux.credlist` (the `admin`/`user1`/`user2` accounts the login checks authenticate with,
  kept in sync with the OS accounts the hardening step creates on each box).
- **[4.5/7] Enable ubuntu password auth on team1.** nakon connects with password auth
  (`ubuntu`/`ubuntu`) and runs `sudo`, so `setup_ubuntu_auth()` turns on `PasswordAuthentication`
  and NOPASSWD sudo — on team1's boxes only, since the other teams don't exist yet.
- **[5/7] Fix DNS + run nakon on team1.** `fix_dns_on_boxes()` repairs `/etc/resolv.conf` (boxes
  boot with it empty), NAT is re-asserted, then the team1 slice of `nakon/config.json` is scp'd
  to the engine and nakon runs there, SSHing into team1's boxes to install services and plant
  misconfigurations.
- **[6/7] Clone team1's boxes to the other teams + run nakon on them.** `clone_team_boxes()`
  `cloud-init clean`s and stops team1's boxes, then full-clones them to each other team via the
  Proxmox API (rewriting each clone's cloud-init IP and bridge), and records the new vmids in
  `competitions/<id>/cloned_vms.json` — these are **not** in Terraform state, so only
  `destroy-competition.py` (which reads that file) can remove them. It re-fixes DNS, re-runs
  `setup_ubuntu_auth()` and service hardening on every team, then runs nakon against team2+.
  (For a single-team event this phase just hardens team1 in place.)
- **[7/7] Seed the competition + create injects.** `seed_and_start()` logs into Quotient's web
  API, sets each team's subnet, and starts the clock; if `competitions/<id>/injects/` exists,
  `create_injects()` uploads them.

Teardown is **`destroy-competition.py`**, not `terraform destroy` on its own: the cloned team2+
boxes were created via the Proxmox API and aren't in Terraform state, so the script first
destroys the vmids in `competitions/<id>/cloned_vms.json` and then runs `terraform destroy`.
It requires `competitions/<id>/teams.json` and `boxes.json` (both written by `deploy()`).
Re-running `create-competition.py` rebuilds everything from scratch — its phase [1/7] tears the
prior range down first, so there's no "update in place."

## Setup steps (do these once, before the first `create-competition.py` run)

1. **Proxmox API token** — Datacenter → Users → add a user, then Permissions → API Tokens →
   add a token. Grant it `PVEAdmin`, or at minimum `VM.Allocate`, `VM.Clone`,
   `VM.Config.All`, `VM.PowerMgmt`, `Datastore.AllocateSpace`, `Datastore.Audit`,
   `Sys.Modify`, `SDN.Use` on path `/`.
2. **SSH keypair** for Terraform/the scoring agent/nakon:
   `ssh-keygen -t ed25519 -f ~/.ssh/range_key -C "range-automation" -N ""`.
3. **Two Proxmox templates**, built and tagged by hand — see README.md's
   ["Adding a template VM"](README.md#adding-a-template-vm) for the full process:
   - A **scoring engine** template with `vm_username` already created, your SSH public key in
     its `authorized_keys`, and passwordless sudo — Terraform never touches its cloud-init at
     clone time, so whatever the template has baked in is permanent.
   - At least one **box** template (cloud-init must work; Terraform overwrites its account on
     every clone with a hardcoded `ubuntu`/`ubuntu`), tagged `template` in Proxmox.
4. **Workstation tooling**: `terraform` CLI, and `pip install toml requests python-dotenv`
   (see README.md's [Prerequisites](README.md#prerequisites-one-time-per-proxmox-host)).
5. **Config file**: `cp .env.example .env`, then fill in the real Proxmox endpoint/token, SSH
   key path, `template_vm_id` (from step 3), and `vm_username`. This is now the *only* file
   with real, mostly-static settings — there's no `terraform.tfvars` anymore. `.env` is
   gitignored; `.env.example` is the committed, placeholder-only version of it. You don't need
   to fill in `boxes_per_team` yourself — `create-competition.py` asks you interactively (see
   pipeline step 3 above) and only needs Proxmox + the templates from step 3 to be reachable.
6. **nakon**: clone https://github.com/CyberDawgsTeam/nakon (or wherever your fork lives) as a
   sibling directory and symlink it in as `nakon/` (`ln -s ../nakon nakon`). It needs its own
   `nakon/.env` (gitignored, **separate from the root `.env`** — not consolidated by design)
   with vulndb connection details:
   ```env
   host=...
   user=...
   password=...
   database=...
   ```
7. **vulndb** reachable from your workstation (`create-competition.py` queries it directly to
   build `nakon/config.json`) *and* from the scoring engine (nakon runs there in deploy phases
   5/6) — the driver scps `nakon/.env` to the scoring engine alongside nakon's other runtime
   files.
8. **Patch vulndb's `bind` script**: `python3 fix-bind-stub-listener.py`. On Debian 13
   systemd-resolved already holds `127.0.0.53:53`, so bind9 loses the race for port 53 and
   never starts — any competition that draws `bind` scores that box down forever. Nothing
   calls this for you: it's a one-time `UPDATE` against the **shared** vulndb, so it is
   deliberately not part of every run. Run it once per database, not once per event.

Once all of that is in place: `python3 create-competition.py`.

## File by file

| File | What it does |
|---|---|
| `create-competition.py` | The entry point and the seven-phase driver. Loads `.env`, drives the interactive prompts (including the Proxmox-template-backed box picker), rewrites the per-event `TF_VAR_*` values, generates `nakon/config.json`, then runs `deploy()`: cleanup → Terraform → engine bootstrap → nakon → cloning → seed/start. |
| `destroy-competition.py` | Teardown. Reads `competitions/<id>/cloned_vms.json` and destroys the API-cloned team2+ boxes (not in Terraform state), then runs `terraform destroy`. Requires that competition's `teams.json` + `boxes.json`, both written by `deploy()`. |
| `competitions/<id>/boxes.json` | The box list (`name`/`template`/`cpu`/`memory_mb`/`disk_gb`/`last_octet`) picked interactively when that competition was created — written by `create-competition.py`, reloaded verbatim when you reuse the competition. |
| `competitions/<id>/teams.json` / `cloned_vms.json` / `credentials.txt` | Written by `deploy()`. `teams.json` is the team→identifier/password map (needed by teardown); `cloned_vms.json` maps each API-cloned team2+ box to its vmid (also needed by teardown); `credentials.txt` (mode 0600) is the authoritative record of the scoreboard URL and the random admin/team passwords. |
| `terraform/main.tf` | The infra Terraform still owns: the team bridges, the scoring VM (vmid `1000`), and **team1's** target VMs, plus a `null_resource` that addresses the engine's team-facing NICs. A comment marks the old Steps A/C/D (package install, Quotient, nakon) as "moved to Python" — those now live in `create-competition.py`. |
| `terraform/scripts/prepare_boxes.py` | Legacy DNS-repair helper from when nakon ran inside `terraform apply`. The live flow no longer invokes it — DNS repair is now `fix_dns_on_boxes()` in `create-competition.py` (phases 5 and 6). |
| `terraform/variables.tf` | Declares every setting, with defaults where it makes sense. Each one is set via the `TF_VAR_<name>` env var of the same name — read this file to know what's configurable. |
| `terraform/outputs.tf` | Packages everything the driver needs (IPs, team list, SSH command) into one JSON blob (`agent_context`) that `create-competition.py` reads via `terraform output -json` after `apply` finishes, in phase 3. |
| `terraform/templates/scoring-init.yaml.tpl` | A cloud-init template — **currently unused**. The scoring engine is cloned from a pre-built template instead (see "Improve" below), so this file is dead code right now. |
| `quotient/setup.py` | `build_event_conf()` turns the box/service map into `event.conf` (Quotient TOML); `seed_and_start()` logs into Quotient's web API as admin, assigns each team its subnet number, and starts the clock; `create_injects()` uploads injects. Called from `deploy()`'s phases 4 and 7. (`build_credlist()` also lives here, but the live driver writes `linux.credlist` inline in `push_event_conf()`.) |
| `proxmox` / `proxmox.pub` | The SSH keypair Terraform/quotient/nakon all use to reach VMs. |
| `.env` / `.env.example` | The single config file for both Terraform (`TF_VAR_*`) and `create-competition.py`. `.env` is gitignored (real secrets); `.env.example` is committed (placeholders only, safe to share). |
| `fix-bind-stub-listener.py` | One-time vulndb patch, run by hand (see setup step 8). Rewrites the `bind` configuration's install script so it disables systemd-resolved's stub listener before starting bind9 — without it bind9 can't bind port 53 on Debian 13 and the box scores down. Mutates the shared database, so it is not wired into `create-competition.py`. |
| `nakon/` | A symlink to your local nakon checkout. `nakon/randomize_config.py` is loaded in-process by `generate_nakon_config()` in `create-competition.py` and runs on your laptop up front — it queries the vulndb and writes `nakon/config.json`. During the deploy the driver `scp`s nakon's runtime files (`deploy.py`, `configurations.py`, `.env`, `config.json`) onto the scoring engine and runs `deploy.py` there (phases 5 and 6). `nakon/.env` (vulndb creds) is its own file, deliberately not merged into the root `.env`. |

## Things that are fake / placeholder right now

- **Passwords baked in**: target VMs get a hardcoded `ubuntu`/`ubuntu` account
  (`main.tf`) because nakon connects with password auth, not keys. Anyone who knows the
  code knows the password to every target box.
- **Quotient's own Postgres/Redis passwords** are hardcoded literals (`postgres_password` /
  `redis_password`) written into the engine's `/opt/quotient/.env` by
  `create-competition.py` (`bootstrap_scoring_engine()` / `push_event_conf()`), not sourced
  from `.env` at all — lower priority than the box password above since they're internal to the
  scoring engine's own docker-compose network, but still worth moving to `.env`-sourced values
  if you want zero hardcoded secrets anywhere.

## What you need to test yourself

1. **The full deploy/destroy cycle once, on a throwaway Proxmox setup**, watching all seven
   phases complete without manual intervention. This is the only way to know the SSH-driven
   bootstrap/nakon/cloning steps in `create-competition.py` actually work end to end.
2. **That your Proxmox templates are correct** — passwordless sudo for `vm_username`,
   your SSH public key in `authorized_keys`, cloud-init enabled. The README warns a
   broken template "fails silently and just leaves you locked out" — confirm on a
   scratch clone before tagging anything `template`.
3. **That Quotient actually shows the right boxes/checks per team** after a run — open
   its web UI and verify scoring, not just that the containers started.
4. **That nakon actually connects and plants misconfigs** — SSH into a target box
   afterward and check the vulnerability nakon was supposed to install is really there.
5. **That a deploy is a full rebuild, not an incremental update.** `deploy()`'s phase [1/7]
   destroys the previous team boxes, the engine, and the team bridges before Terraform runs, so
   re-running `create-competition.py` to "add a team" wipes and rebuilds the whole range (and
   regenerates every password). Adding a team to a *live* event therefore has to be done by
   hand — confirm the by-hand path (clone the boxes the way `clone_team_boxes()` does and
   re-push `event.conf`) before you rely on it.

## What to improve to make this actually work with Quotient + nakon

- **Decide if nakon needs key-based SSH instead of the hardcoded `ubuntu`/`ubuntu`**
  password — if nakon supports key auth, switching removes a real weak point (every box,
  every event, same password).
- **Give the `Sql` check a database user it can actually log in as.** The `linux.credlist`
  `push_event_conf()` pushes lists OS accounts, which satisfies the Ssh/Smtp/Imap checks (they
  authenticate through PAM) but not Sql, which authenticates against the database's own user
  table. The `fix_services_on_boxes()` hardening step now creates matching MySQL/MariaDB users
  from that same credlist — worth verifying it lands before the first scoring round, since a
  missing DB user scores a perfectly healthy server down. Same question for any future check
  type with `CredLists`.
- **Extend `_SERVICE_TO_CHECK` as vulndb grows.** `build_event_conf()` maps nakon's service
  names to Quotient checks through that table; anything in vulndb that isn't in it gets no
  check at all and only logs a warning. Worth an audit against the `configurations` table.
- **Remove or use `scoring-init.yaml.tpl`** so the repo doesn't have two unreconciled
  ways of describing the scoring engine.
- **Move Quotient's hardcoded Postgres/Redis passwords into `.env`-sourced Terraform
  variables** (see "fake/placeholder" above).
