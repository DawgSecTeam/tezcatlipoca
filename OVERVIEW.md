# What this project actually does

You're spinning up a hacking-competition "range": one scoring server (Quotient) plus a
set of target boxes per team, all on Proxmox. Terraform builds the VMs and networking,
`create-competition.py` drives the whole thing end to end, and a separate tool called
**nakon** (not part of this repo — cloned from GitHub at runtime) SSHes into each target
box to break it in specific, scored ways.

Team bridges have no uplink of their own. The scoring engine is the only machine with a
network card on every team's subnet, so it also doubles as the machine that runs nakon — and
as every team's gateway to the internet, which it NATs. That also means it can route team to
team, so what actually isolates teams is `range-firewall.sh` (`main.tf` Step C), not the
bridges being empty.

## The pipeline, in order

`create-competition.py` is the single entry point — you run it, answer its prompts, and
everything below happens without further intervention:

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
4. It rewrites `TF_VAR_event_name`, `TF_VAR_quotient_admin_password`, `TF_VAR_teams`, and
   `TF_VAR_boxes_per_team` in `.env` in place (`update_env()`) — these four are the only
   per-event values; everything else in `.env` (Proxmox connection, SSH keys, the scoring
   engine's own template/account) is set once and left alone.
5. It generates nakon's machine + vuln list itself: `generate_nakon_config()` loads
   `nakon/randomize_config.py` directly (no subprocess — nakon has no `__init__.py`, so it's
   imported via `importlib`), queries the vulndb's `configurations` table, picks services/vulns
   per box scaled by the difficulty you chose, and writes `nakon/config.json`. This used to be
   a separate manual step you ran yourself before `apply` — it isn't anymore.
6. It runs `terraform init && terraform apply -parallelism=1 -auto-approve` in `terraform/`.
   Within that one `apply`:
   - Terraform creates one isolated network bridge per team, clones the scoring-engine VM,
     and clones N target VMs per team from the box templates picked in step 3.
   - Terraform SSHes into the scoring engine and installs Docker, clones Quotient + nakon,
     starts Quotient (paused).
   - `terraform/scripts/prepare_boxes.py` is scp'd to the scoring engine and run against every
     target box first: it repairs DNS (boxes boot with an empty `/etc/resolv.conf`) and
     refreshes the package cache. It **fails the apply** if a box still can't resolve, because
     nakon installs everything with `apt-get` and reports success either way — a box that
     skips this silently gets nothing installed and shows every service down.
   - `nakon/config.json` (from step 5) gets scp'd to the scoring engine and nakon runs there,
     SSHing into every target box to actually install services and plant misconfigurations.
7. Once `apply` returns, `push_event_conf()` runs **on your laptop**: it reads
   `terraform output -json` for the scoring engine's real (DHCP-leased) IP and the rest of
   `agent_context`, then `quotient/setup.py`'s `build_event_conf()` turns that into
   `event.conf` (Quotient's scoring rules), written straight into the competition's folder.
   `build_credlist()` writes `linux.credlist` next to it — the accounts Quotient's login
   checks authenticate with. It has to match the account `main.tf` puts on every box, so it's
   generated here rather than copied from Quotient's `.example` (whose accounts exist nowhere,
   which scores healthy services as down).
8. `push_event_conf()` pushes `event.conf` + `linux.credlist` to the scoring engine and
   restarts Quotient to pick them up.
9. `quotient/setup.py`'s `seed_and_start()` logs into Quotient's web API, sets each team's
   subnet number, and starts the competition clock.
10. `print_summary()` prints the scoreboard URL, the admin login, every team's login +
    subnet, and the scoring engine's SSH command — the one place all of this is shown
    together, since several of these (random team/admin passwords in particular) aren't
    recoverable from anywhere else after the screen scrolls past.

Teardown is `terraform destroy -parallelism=1`. Re-running `apply` after `destroy` rebuilds
everything from scratch — there's no "update in place."

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
   build `nakon/config.json`) *and* from the scoring engine (nakon re-runs there during
   `terraform apply`) — same `nakon/.env` is scp'd to the scoring engine in `main.tf`.
8. **Patch vulndb's `bind` script**: `python3 fix-bind-stub-listener.py`. On Debian 13
   systemd-resolved already holds `127.0.0.53:53`, so bind9 loses the race for port 53 and
   never starts — any competition that draws `bind` scores that box down forever. Nothing
   calls this for you: it's a one-time `UPDATE` against the **shared** vulndb, so it is
   deliberately not part of every run. Run it once per database, not once per event.

Once all of that is in place: `python3 create-competition.py`.

## File by file

| File | What it does |
|---|---|
| `create-competition.py` | The entry point. Loads `.env`, drives the interactive prompts (including the Proxmox-template-backed box picker), rewrites the per-event `TF_VAR_*` values, generates `nakon/config.json`, runs Terraform, then pushes/starts Quotient. |
| `competitions/<id>/boxes.json` | The box list (`name`/`template`/`cpu`/`memory_mb`/`disk_gb`/`last_octet`) picked interactively when that competition was created — written by `create-competition.py`, reloaded verbatim when you reuse the competition. |
| `terraform/main.tf` | Everything infra-side: bridges, scoring VM, target VMs, then SSHes in to install software and run nakon. This is the core of the provisioning. |
| `terraform/scripts/prepare_boxes.py` | Runs on the scoring engine from `main.tf`'s Step D, immediately before nakon: repairs DNS and refreshes the package cache on every target box, and aborts the apply if any box still can't resolve. Exists because nakon swallows its own install failures. |
| `terraform/variables.tf` | Declares every setting, with defaults where it makes sense. Each one is set via the `TF_VAR_<name>` env var of the same name — read this file to know what's configurable. |
| `terraform/outputs.tf` | Packages everything `quotient/setup.py` needs (IPs, passwords, team list) into one JSON blob (`agent_context`) that `create-competition.py` reads via `terraform output -json` after `apply` finishes. |
| `terraform/templates/scoring-init.yaml.tpl` | A cloud-init template — **currently unused**. The scoring engine is cloned from a pre-built template instead (see "Improve" below), so this file is dead code right now. |
| `quotient/setup.py` | `build_event_conf()` turns Terraform's output into `event.conf` (Quotient TOML); `build_credlist()` writes the accounts its login checks authenticate with; `seed_and_start()` logs into Quotient's web API as admin, assigns each team its subnet number, and clicks "start competition". All called from `create-competition.py` after `terraform apply` returns. |
| `proxmox` / `proxmox.pub` | The SSH keypair Terraform/quotient/nakon all use to reach VMs. |
| `.env` / `.env.example` | The single config file for both Terraform (`TF_VAR_*`) and `create-competition.py`. `.env` is gitignored (real secrets); `.env.example` is committed (placeholders only, safe to share). |
| `fix-bind-stub-listener.py` | One-time vulndb patch, run by hand (see setup step 8). Rewrites the `bind` configuration's install script so it disables systemd-resolved's stub listener before starting bind9 — without it bind9 can't bind port 53 on Debian 13 and the box scores down. Mutates the shared database, so it is not wired into `create-competition.py`. |
| `nakon/` | A symlink to your local nakon checkout. Unlike the rest of nakon (which Terraform clones fresh onto the scoring engine), `nakon/randomize_config.py` is loaded in-process by `generate_nakon_config()` in `create-competition.py` and runs on your laptop *before* `terraform apply` — it queries the vulndb and writes `nakon/config.json`, which `main.tf` then pushes to the scoring engine. `nakon/.env` (vulndb creds) is its own file, deliberately not merged into the root `.env`. |

## Things that are fake / placeholder right now

- **Passwords baked in**: target VMs get a hardcoded `ubuntu`/`ubuntu` account
  (`main.tf`) because nakon connects with password auth, not keys. Anyone who knows the
  code knows the password to every target box.
- **Quotient's own Postgres/Redis passwords** are hardcoded literals (`changeme_in_prod`) in
  `main.tf`'s Step C, not sourced from `.env` at all — lower priority than the box password
  above since they're internal to the scoring engine's own docker-compose network, but still
  worth moving to `.env`-sourced variables if you want zero hardcoded secrets anywhere.

## What you need to test yourself

1. **The full apply/destroy cycle once, on a throwaway Proxmox setup**, watching it
   complete without manual intervention. This is the only way to know the SSH
   provisioning steps in `main.tf` actually work end to end.
2. **That your Proxmox templates are correct** — passwordless sudo for `vm_username`,
   your SSH public key in `authorized_keys`, cloud-init enabled. The README warns a
   broken template "fails silently and just leaves you locked out" — confirm on a
   scratch clone before tagging anything `template`.
3. **That Quotient actually shows the right boxes/checks per team** after a run — open
   its web UI and verify scoring, not just that the containers started.
4. **That nakon actually connects and plants misconfigs** — SSH into a target box
   afterward and check the vulnerability nakon was supposed to install is really there.
5. **Adding a team mid-event** (by hand-editing `TF_VAR_teams` in `.env`, *not* by re-running
   `create-competition.py` — see its team-password caveat in README.md) and **adding a box
   type** — the README claims these are incremental (`apply` only touches the new pieces), but
   this isn't proven; verify nothing else gets recreated/destroyed by accident.

## What to improve to make this actually work with Quotient + nakon

- **Decide if nakon needs key-based SSH instead of the hardcoded `ubuntu`/`ubuntu`**
  password — if nakon supports key auth, switching removes a real weak point (every box,
  every event, same password).
- **Give the `Sql` check a database user it can actually log in as.** `build_credlist()` emits
  the boxes' OS account, which satisfies the Ssh/Smtp/Imap checks (they authenticate through
  PAM) but not Sql, which authenticates against the database's own user table. Until nakon's
  mysql/mariadb install script creates a matching DB user, those checks score down on a
  perfectly healthy server. Same question for any future check type with `CredLists`.
- **Extend `_SERVICE_TO_CHECK` as vulndb grows.** `build_event_conf()` maps nakon's service
  names to Quotient checks through that table; anything in vulndb that isn't in it gets no
  check at all and only logs a warning. Worth an audit against the `configurations` table.
- **Remove or use `scoring-init.yaml.tpl`** so the repo doesn't have two unreconciled
  ways of describing the scoring engine.
- **Move Quotient's hardcoded Postgres/Redis passwords into `.env`-sourced Terraform
  variables** (see "fake/placeholder" above).
