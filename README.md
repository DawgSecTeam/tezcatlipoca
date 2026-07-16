# Quotient + nakon range automation

Proxmox-based scoring range. Terraform provisions the infra, create-competition.py generates
Quotient's scoring config and nakon's machine list, then nakon SSHes into each box to install
services and deploy misconfigs. Team bridges have no uplink of their own, so the scoring engine
is the only thing with a NIC on every team's network — it's also where nakon actually runs, and
it's what NATs each team out to the internet. Because it sits on every team bridge it *could*
route team to team; `range-firewall.sh` (installed by `main.tf` Step C) is what actually keeps
teams apart, not the empty bridges.

## Layout

```
terraform/   Infra: bridges, scoring VM, team VMs, orchestration (main.tf)
quotient/    setup.py — builds event.conf and seeds/starts the competition via Quotient's API
nakon/       symlink to ~/dev/nakon — box configuration tool, developed in its own repo.
             create-competition.py loads randomize_config.py from here to write config.json
             before calling `terraform apply` — you don't run it yourself anymore.
```

## Prerequisites (one-time, per Proxmox host)

**API token** — Datacenter → Users → add a user, then Permissions → API Tokens → add a token
for it. Grant it (Datacenter → Permissions → Add → User Permission, path `/`) the `PVEAdmin`
role, or at minimum `VM.Allocate`, `VM.Clone`, `VM.Config.All`, `VM.PowerMgmt`,
`Datastore.AllocateSpace`, `Datastore.Audit`, `Sys.Modify`, `SDN.Use`.

**Templates** — see [Adding a template VM](#adding-a-template-vm) below for the full process.

**SSH keypair** — used by Terraform, the agent, and nakon:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/range_key -C "range-automation" -N ""
```

**Workstation tooling**:
```bash
# Terraform (HashiCorp apt repo)
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform

# Python deps for create-competition.py / quotient/ (runs on your workstation)
pip install toml requests python-dotenv
```

**Config file** — copy the template and fill in your real values:
```bash
cp .env.example .env
```
`.env` is gitignored — it's the only place real Proxmox creds, SSH key path, and passwords
live. See [Configure the event](#configure-the-event) below for what each value means.

**vulndb** — nakon's MySQL database of `vulnerabilities`/`misconfigs` (see `nakon/README.md`
for the schema). nakon runs *on the scoring engine*, not your workstation, so vulndb must be
reachable from the scoring engine's management network. Put the connection details in
`nakon/.env` (gitignored):
```env
host=...
user=...
password=...
database=...
```

Then patch the `bind` configuration in that database once, before your first run:
```bash
python3 fix-bind-stub-listener.py
```
On Debian 13 systemd-resolved already owns `127.0.0.53:53`, so bind9 fails to start and any
box that draws `bind` scores down permanently. This is a one-time `UPDATE` against the shared
vulndb — nothing runs it for you, and it only needs doing once per database.

## Adding a template VM

Every VM Terraform creates is a full clone of a Proxmox template you build by hand
beforehand. There are two different roles, and `main.tf` treats their credentials
differently — get this distinction right or a clone will boot with no working login.

**Scoring engine template** (`var.template_vm_id`) — Terraform never touches its cloud-init
at clone time (no `initialization` block on `scoring_engine` in `main.tf`), so whatever
account/key/sudo access the template has baked in is permanently what every clone gets.

**Box templates** (`boxes_per_team[].template`) — Terraform's own `initialization.user_account`
block creates/overwrites a `ubuntu` account, pushes `var.ssh_public_key`, and sets the password
to `ubuntu` on *every* clone (nakon connects with password auth, hence the fixed password).
The template just needs cloud-init itself to be working so that override actually lands —
whatever account the template ships with otherwise doesn't matter.

### Minimum specifications

| Role | vCPU | RAM | Disk | Notes |
|------|------|-----|------|-------|
| Scoring engine | 4 | 4 GB | 40 GB | hard-coded in `main.tf` |
| Box template | ≥ 1 | ≥ 1 GB | ≥ 10 GB | see `/tmp` note below |

**`/tmp` sizing:** Linux defaults to a `tmpfs` at 50% of RAM. `deploy.py` stages each box's
attachments in `/tmp/` on the remote machine before running the deploy script. A box with
512 MB RAM has only ~256 MB of `/tmp` headroom — a large attachment or leftover files from
a previous failed deploy can fill it entirely, breaking both `apt` post-install scripts
(`No space left on device`) and the next SFTP transfer (`size mismatch in put!`). If any
attachment in vulndb exceeds that limit, increase `memory_mb` for the affected box type in
`boxes_per_team`.

### 1. Build the base VM

Create a new VM in Proxmox from a cloud-init-capable image — an official Ubuntu/Debian cloud
image is the path of least resistance. If you're installing from an ISO instead, install the
`cloud-init` package yourself and confirm it's enabled. Either way, also install
`qemu-guest-agent` (required for the scoring engine template specifically — `main.tf` reads
its DHCP-leased IP back through the guest agent; harmless to include on box templates too).

Boot it once and confirm cloud-init actually ran:
```bash
cloud-init status --long   # should say "status: done"
```

### 2. Seed credentials (scoring engine template only)

Skip this step for box templates — Terraform pushes their account dynamically (see above).

While the VM is still running, before converting it:
- Create/confirm an account named exactly `var.vm_username`.
- Add your automation public key (`var.ssh_public_key`) to that account's
  `~/.ssh/authorized_keys`.
- Grant it passwordless sudo, e.g. `echo "<user> ALL=(ALL) NOPASSWD:ALL" | sudo tee
  /etc/sudoers.d/90-range-automation`.

Confirm both survive a reboot before moving on.

### 3. Convert to template and tag it

```bash
qm template <vmid>
qm set <vmid> --tags template
```
(or Proxmox UI: right-click → Convert to Template, then Summary → Tags → add `template`.)
`main.tf` looks up box templates by exact VM **name** among VMs carrying this tag — note the
template's exact name, the box picker (see [Configure the event](#configure-the-event)) will
list it for you, but only once it's actually tagged.

### 4. Verify before trusting it

Clone it manually once (`qm clone <template-vmid> <scratch-vmid> --full`), boot the clone, and
check:
- Scoring-engine-style template: `ssh <vm_username>@<ip>` with your key works, and `sudo -n
  true` succeeds (no password prompt).
- Box-style template: just confirm `cloud-init status --long` says `done` — Terraform's own
  clone will exercise the actual account creation.

Destroy the scratch clone once confirmed. A template with broken/disabled cloud-init fails
silently when Terraform clones it for real and just leaves you locked out.

### 5. Wire it in

- Scoring engine: set `TF_VAR_template_vm_id` in `.env` to the template's numeric Proxmox VM
  ID — this one's global, not per-competition.
- New box type: nothing to wire up by hand. `create-competition.py`'s box picker (see
  [Configure the event](#configure-the-event)) queries Proxmox for tagged templates and will
  offer this one by name the next time you create or reuse a competition.

## Configure the event

Everything else lives in `.env` (gitignored — copy `.env.example` and edit, never commit real
values). Terraform reads each of these straight out of the environment via Terraform's
`TF_VAR_<name>` convention — there's no `terraform.tfvars` file at all anymore. `.env` is the
single place to configure both Terraform and `create-competition.py`.

| Variable (`.env` key is `TF_VAR_<name>`) | What it is |
|---|---|
| `proxmox_endpoint` | Proxmox API URL, e.g. `https://10.0.0.10:8006/` |
| `proxmox_api_token` | `user@realm!tokenid=uuid` from Prerequisites |
| `proxmox_node` | Proxmox node name (Proxmox UI → Datacenter) |
| `template_vm_id` | VM ID of the template the **scoring engine** clones from |
| `ssh_public_key` / `ssh_private_key_path` | Keypair from Prerequisites |
| `vm_username` | Account baked into the **scoring engine** template via cloud-init (see [Adding a template VM](#adding-a-template-vm)); box templates always get a separate hardcoded `ubuntu` account from Terraform regardless of this value |
| `quotient_admin_password` | Password for Quotient's `admin` account — randomly generated by `create-competition.py` each run; orchestration logs in with it automatically once Quotient is up |
| `event_name` | Display name for the event |
| `teams` | JSON map of team key → `{identifier, password}` — `identifier` is the subnet's third octet, e.g. `{"team1":{"identifier":"1","password":"hunter2"}}` |
| `boxes_per_team` | JSON list of boxes cloned per team, max 10 (VM IDs allow each team a stride of 10 — see `MAX_BOXES_PER_TEAM`). `template` must match a tagged Proxmox template name; `disk_gb` is optional (omit or `null` to keep the template's own disk size — Proxmox cannot shrink, so a value under the template's own size fails the clone) |

`event_name`, `quotient_admin_password`, `teams`, and `boxes_per_team` are all rewritten
automatically by `create-competition.py` every time you create or reuse a competition — edit
them by hand in `.env` only if you're running `terraform apply` directly without going through
that script. Boxes are different per event on purpose (that's the point of nakon scaling
difficulty per box) — `create-competition.py` queries Proxmox for templates tagged `template`
and walks you through picking boxes interactively each time, then saves the result as
`competitions/<id>/boxes.json` so reusing that competition later replays the exact same boxes
instead of whatever's currently sitting in `.env`.

Teams are **not** picked interactively — you're only asked how many teams there are.
`collect_teams()` auto-names them `team1`, `team2`, ... with identifiers `101`, `102`, ... and
a fresh random password each run. That includes teams that already existed in a previous run
of the same competition — see the "Add a team mid-event" caveat below before you rely on this
for a live event.

Box `name` does **not** decide what gets scored — the services nakon randomly picks for that
box do. `generate_nakon_config()` records them in `competitions/<id>/box_services.json`, and
`build_event_conf()` maps each one to a Quotient check through `_SERVICE_TO_CHECK` in
`quotient/setup.py` (apache/nginx → Web, bind → Dns, sshd → Ssh, and so on). A service in
vulndb whose name isn't in that table gets no check and logs a warning — extend the table if
you add one. Box `name` only matters in one place: a box named `dns*` becomes its team's
resolver (`main.tf`).

## Run it

```bash
python3 create-competition.py
```

This is the actual entry point — see [OVERVIEW.md](OVERVIEW.md) for exactly what it does step
by step. In short: it loads `.env`, asks whether to reuse an existing competition or create a
new one, asks how many teams (it names/passwords them for you, see [Configure the
event](#configure-the-event)), generates nakon's machine + vuln list itself (the same logic as
`nakon/randomize_config.py`, called in-process — no separate manual step), then runs
`terraform init && terraform apply` and pushes/starts Quotient once infra is up. A few minutes
per box; expect most of the time in package installs and image builds. At the end it prints a
summary with the scoreboard URL, admin login, every team's login, and the scoring engine's SSH
command — copy this down, it's the only place team/admin passwords are shown.

**Teardown**: `cd terraform && terraform destroy -parallelism=1` (templates aren't touched).

**Add a team mid-event**: re-running `create-competition.py` with a higher team count
regenerates **every** team's password, not just the new one — fine before an event starts, bad
once teams are already playing on their current credentials. To add a team without disturbing
existing ones, append to `TF_VAR_teams` in `.env` by hand (leave existing entries untouched)
and `terraform apply` directly — only the new bridge is created, plus `null_resource.team_nics`
re-runs to give the engine an address on it. `null_resource.orchestrate` deliberately does
*not* re-run (its Step A re-clones Quotient and its Step D re-runs nakon, which would wipe a
live event), so the new team gets no boxes from this — clone them yourself, the way
`clone_team_boxes()` does, and re-push `event.conf` so Quotient knows about the team.

**Add a box type**: see [Adding a template VM](#adding-a-template-vm) to build/tag the template
— then it just shows up as an option in `create-competition.py`'s box picker. New boxes are
created for every team.

**Running `terraform` directly** (skipping `create-competition.py`) — Terraform doesn't load
`.env` itself, so export it into your shell first. Plain `source .env` breaks on values with
spaces/`!`/JSON braces (several of these have all three), so load it through python-dotenv
instead, the same parser `create-competition.py` uses:
```bash
cd terraform
eval "$(python3 -c "
from dotenv import dotenv_values
for k, v in dotenv_values('../.env').items():
    print(f'export {k}={v!r}')
")"
terraform init
terraform apply -parallelism=1   # avoids Proxmox clone lock errors
```

## Secrets

Never commit `.env`, `nakon/.env`, `nakon/config.json`, or `competitions/*/event.conf` — all
gitignored already (`.env.example` is the committed, placeholder version of `.env`, safe to
share). For shared environments, prefer real env vars over writing secrets to disk at all:
```bash
export TF_VAR_proxmox_api_token="..."
export TF_VAR_quotient_admin_password="..."
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| VM clone lock errors | Proxmox can't clone two VMs at once | Always use `-parallelism=1` |
| Template not found in lookup | Not tagged `template`, or the template name you typed into the box picker doesn't match exactly | Check tag + exact VM name in Proxmox UI |
| Can't SSH into a freshly cloned box | Template's cloud-init is broken/disabled | Verify on a scratch clone (`cloud-init status --long`) before tagging as a template |
| `apt-get install` fails or hangs on a fresh clone | Racing `unattended-upgrades`' first-boot run for the dpkg lock | Already retried automatically (60×2s); rerun apply if it still loses the race |
| Quotient panics on startup re: credlist | A box check references a credlist that was never staged | `create-competition.py` pushes `linux.credlist` alongside `event.conf`, so the two always agree — check `config/credlists/` on the scoring engine if not |
| `terraform apply` fails with "missing nakon/config.json" | Running `terraform apply` directly instead of via `create-competition.py` (which generates it for you) | Run `python3 create-competition.py`, or generate it manually: `cd nakon && python3 randomize_config.py`, then re-apply |
| `randomize_config.py` or nakon: MySQL access denied / connection refused | vulndb unreachable (from your laptop when generating config.json, or from the scoring engine when nakon runs it), or `nakon/.env` creds/db name don't match actual grants | Confirm reachability, check `SHOW GRANTS`, fix `nakon/.env` |
| nakon can't SSH into a box | Box still booting, or its template's account/password doesn't match what `randomize_config.py` assumes | Check `boxes_per_team[].template`'s cloud-init account |
| Bridge creation fails (403) | API token missing `Sys.Modify` | Re-add the permission at the right path/level |
| Everything went down at once, after a scoring-engine reboot | The forwarding/NAT rules didn't get re-applied — Docker rebuilds `FORWARD` (policy `DROP`) on every start | `sudo systemctl status range-firewall.service` on the engine; `sudo systemctl restart range-firewall.service` re-applies them (the script is idempotent). It's ordered after `docker.service` and should do this automatically at boot |
| A team can reach another team's boxes | `range-firewall.sh` isn't applied — the empty bridges don't isolate anything by themselves, since the engine has a NIC on every team bridge and forwards between them | `sudo /usr/local/sbin/range-firewall.sh` on the engine, then check `sudo iptables -L FORWARD -n --line-numbers` for the `192.168.0.0/16 → 192.168.0.0/16 DROP` rule |
| `/tmp` full on a team VM — `No space left on device` in apt output, or `size mismatch in put!` from `deploy.py` | Stale files from a previous failed deploy accumulated in `/tmp` (a RAM-backed tmpfs); `deploy.py` does not clean up after itself | SSH into the scoring engine, then into the affected machine using credentials from `config.json`, and run `find /tmp -maxdepth 1 -type f -delete`; re-run `create-competition.py`. Also fix `deploy.py` in the `nakon` repo to remove `/tmp/<attachment>` after each script run. |
| `terraform apply` fails at `[prep] … box(es) not ready` | Cloud-init's `dns.servers` is silently ignored on Debian with static IPs, leaving `/etc/resolv.conf` empty. Step D's `prepare_boxes.py` repairs it before nakon runs and refuses to continue if a box still can't resolve — nakon would install nothing there and still report success | Read which box failed and why in the `[prep]` output. To debug: SSH through the scoring engine to that box and check `cat /etc/resolv.conf`, `cat /etc/systemd/resolved.conf.d/upstream.conf`, and `getent hosts deb.debian.org`. Re-run `create-competition.py` once fixed |
| Services all show down on the scoreboard | First check round only lands `Delay`+`Jitter` (~70 s) after the summary prints — wait it out first. If they stay down, nakon installed nothing (see the `[prep]` row above), or a login check is authenticating with credentials no box has | On a box: `ss -ltnp` to see whether the service is even listening. If it is, the check is failing auth — compare `/opt/quotient/config/credlists/linux.credlist` on the scoring engine against the box's real accounts. `Sql` checks need a matching *database* user, which nakon's install script has to create |
