# Usage — people

Interactive, human-operator guide: one-time Proxmox/workstation setup, running a deploy by
answering its prompts, and day-to-day operation. For scripted/non-interactive/agent-driven
usage (CLI flags, pre-authored configs), see [usage-agents.md](usage-agents.md). For what the
project is and how it's architected, see the [README](../README.md).

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

**vulndb** — nakon's MySQL database of `vulnerabilities`/`misconfigs` (see
`vendor/nakon/README.md` for the schema). nakon's build/randomize run on your workstation, so
vulndb must be reachable from there (deploy runs later on the scoring engine from the bundle —
no vulndb needed there). Put the connection details in `vendor/nakon/.env` (gitignored):
```env
host=...
user=...
password=...
database=...
```

**Known catalog bugs already patched (2026-08-12)** — the `configurations` rows below were
buggy `script` content in the shared vulndb itself (not this repo's code) and have already been
corrected there directly, so nothing needs running against the vulndb this project currently
points at. Recorded here only so a **brand-new** vulndb instance (freshly loaded from
`nakon/vulndb/seed.sql`, which still has the original buggy scripts) knows what to check for and
fix by hand before its first run:
- `bind` — Debian 13's systemd-resolved already owns `127.0.0.53:53`, so bind9 failed to start;
  its `apt` branch never ran `apt-get update` before install (stale-index 404s); and current
  bind9 packaging ships `bind9.service` as a symlink to `named.service` (`systemctl enable`
  refuses to operate on a linked unit) with no default zones at all.
- `journal-disk-full` — wrote a drop-in under `/etc/systemd/journald.conf.d/` without creating
  that directory first, failing outright on images where it didn't already exist.
- `apache`, `install-package` — same missing-`apt-get update` defect class as `bind`, causing
  stale-index install failures.
- `nfs-no-root-squash` — appended its `/etc/exports` line unconditionally on every run (nakon
  applies a box's plan twice per competition), so the second pass errored on a duplicate entry.
- `ssh-password-auth` — its restart branch was indented with a literal `\t` (backslash-t) rather
  than a real tab, so bash ran the single bogus token `tsystemctl` instead of restarting sshd.

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

### 3. Reset its cloud-init state, then convert to template and tag it

**Do this immediately before shutting down — it's the step that's easy to skip and hardest to
notice you skipped.** If cloud-init ever ran on this VM before now (step 1's "boot it once and
confirm cloud-init actually ran" check, or any manual boot), it cached this instance's ID and
recorded that network setup already happened. Proxmox clones don't reset that — every future
clone would look like "the same instance rebooting" to cloud-init, which then correctly (per
its own default policy) skips re-applying network config, even though each clone needs a
*different* IP. This is silent: `cloud-init status --long` still reports `done`, nothing errors,
the clone just never gets its IP.

```bash
sudo cloud-init clean --logs --seed
```

Then:
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
- Box-style template: confirm `cloud-init status --long` says `done`, **and** check the actual
  IP landed (`ip -br addr` on the clone, or via the Proxmox guest agent from the host) —
  `status: done` on its own isn't sufficient proof; it also reports `done` in the stale-instance-
  id case from step 3 above, where the network never actually got (re)configured. The real test
  is that the clone's IP matches what you set in its `ipconfig0`, not just that cloud-init exited
  cleanly.

Destroy the scratch clone once confirmed. A template with broken/disabled cloud-init, or one
that skips reapplying network config because its instance-id was never reset, fails silently
when Terraform clones it for real and just leaves you locked out.

### 5. Wire it in

- Scoring engine: set `TF_VAR_template_vm_id` in `.env` to the template's numeric Proxmox VM
  ID — this one's global, not per-competition.
- New box type: nothing to wire up by hand. `create-competition.py`'s box picker (see
  [Configure the event](#configure-the-event)) queries Proxmox for tagged templates and will
  offer this one by name the next time you create or reuse a competition.

### Windows box templates

None of the above applies to Windows — there's no cloud-init, so the process is different, not
just similarly-shaped. `terraform/main.tf`'s `initialization` block is skipped entirely for any
template whose name contains `win` (the same substring convention nakon's own `os_to_platform()`
uses to route catalog configs, so name it consistently and every layer agrees). Windows boxes get
their IP/gateway/DNS and local Administrator password set post-clone instead, over the QEMU guest
agent (`bootstrap_windows_box()` in `create-competition.py`) — nakon authenticates by password
(see `nakon/deploy/ssh.py`), not a key, so there's no equivalent of pushing `ssh_public_key`.

Building the template itself, verified end-to-end producing `windows-server-fix`:

1. **Install unattended.** Windows Server Evaluation ISOs are free from Microsoft
   (`https://go.microsoft.com/fwlink/p/?LinkID=2195280` for 2022 at time of writing — confirm the
   redirect target is a real `...download.prss.microsoft.com` signed ISO before trusting it).
   Drive the install with an `autounattend.xml` (floppy image via `qm set <vmid> --args '-fda
   /path/to/autounattend.img'` — attaching it as a second CD-ROM did *not* get auto-detected in
   testing, the floppy path is the reliable one). **The one bug that will burn you**: declare
   `xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"` on the root `<unattend>`
   element if you use any `wcm:action` attribute — omitting it fails silently past the very first
   WinPE language-select screen (that screen never auto-skips even with a *working* answer file,
   don't mistake it for detection failure) and only surfaces as "could not parse... line N column
   M" once you click through to "Install now" manually. Boot with SeaBIOS + a SATA disk + an
   `e1000` NIC, not virtio — avoids needing driver injection during WinPE's fragile textmode
   phase; install the real virtio-win guest tools afterward instead (next step).
2. **Bootstrap over the guest agent** (works before there's any network, same as the Linux
   template-fix workflow): `msiexec /i <virtio-win-iso-drive>:\virtio-win-gt-x64.msi /qn
   /norestart` — the QEMU guest agent service reports "running" without this, but doesn't
   actually work (no vioserial driver bound) until the real virtio drivers are installed. Then
   `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, start+enable `sshd`, open the
   firewall for :22.
3. **Sysprep it.** `sysprep /generalize /oobe /shutdown /unattend:<path>` where that unattend.xml
   sets `<ComputerName>*</ComputerName>` (specialize pass) so every future clone gets a fresh
   random hostname/SID with zero manual steps, and skips OOBE the same way the install-time
   answer file did. **The other bug that will burn you**: sysprep fails immediately (error
   0x3cf2, "Package Microsoft.MicrosoftEdge.Stable... was installed for a user, but not
   provisioned for all users") on current Server images — the well-known Edge-blocks-sysprep
   issue. Fix before sysprepping: `Get-AppxPackage -AllUsers -Name "*MicrosoftEdge*" |
   Remove-AppxPackage -AllUsers` (the broader `Get-AppxPackage -AllUsers | Remove-AppxPackage
   -AllUsers` alone was not sufficient in testing — target Edge explicitly and confirm it's
   actually gone before retrying).
4. **Tag it** the same as any other template: `qm template <vmid>; qm set <vmid> --tags
   template`.

Verify the same way as step 4 above, but over the guest agent instead of SSH (no network is
guaranteed yet on a fresh clone): confirm `$env:COMPUTERNAME` differs from the template's own
build-time name, and that `(Get-Service sshd).Status`/`(Get-Service QEMU-GA).Status` both read
`Running` with no manual intervention.

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
| `vm_username` | Account baked into the **scoring engine** template via cloud-init (see [Adding a template VM](#adding-a-template-vm)) — unrelated to box logins, which are themeable separately (see below) |
| `box_username` | Login account Terraform's cloud-init creates on every **team box** clone. Rewritten automatically by `create-competition.py` from `competitions/<id>/users.json` (default `ubuntu` when that file doesn't exist) — see below, don't edit this key by hand |
| `quotient_admin_password` | Password for Quotient's `admin` account — randomly generated by `create-competition.py` each run; orchestration logs in with it automatically once Quotient is up |
| `event_name` | Display name for the event |
| `teams` | JSON map of team key → `{identifier, password}` — `identifier` is the subnet's third octet, e.g. `{"team1":{"identifier":"1","password":"hunter2"}}` |
| `boxes_per_team` | JSON list of boxes cloned per team, max 10 (VM IDs allow each team a stride of 10 — see `MAX_BOXES_PER_TEAM`). `template` must match a tagged Proxmox template name; `disk_gb` is optional (omit or `null` to keep the template's own disk size — Proxmox cannot shrink, so a value under the template's own size fails the clone) |

`teams` and `boxes_per_team` are rewritten automatically in `.env` by `create-competition.py`
every time you create or reuse a competition (`update_env()`). `event_name` comes from the
competition's `Compfile`, and `quotient_admin_password` is generated fresh in memory each run
(saved to `competitions/<id>/credentials.txt`, not written back to `.env`) — edit the `.env`
copies of these by hand only if you're running `terraform apply` directly without going through
the driver. Boxes are different per event on purpose (that's the point of nakon scaling
difficulty per box) — `create-competition.py` queries Proxmox for templates tagged `template`
and walks you through picking boxes interactively each time, then saves the result as
`competitions/<id>/boxes.json` so reusing that competition later replays the exact same boxes
instead of whatever's currently sitting in `.env`.

Teams are **not** picked interactively — you're only asked how many teams there are.
`collect_teams()` auto-names them `team1`, `team2`, ... with identifiers `101`, `102`, ... and
a fresh random password each run. That includes teams that already existed in a previous run
of the same competition — see the "Add a team mid-event" caveat below before you rely on this
for a live event.

Box `name` does **not** decide what gets scored — the services picked for that box do.
`generate_nakon_config()` records them in `competitions/<id>/box_services.json`, and
`build_event_conf()` maps each one to a Quotient check through `_SERVICE_TO_CHECK` in
`quotient/setup.py` (apache/nginx → Web, bind → Dns, sshd → Ssh, and so on). A service in
vulndb whose name isn't in that table gets no check and logs a warning — extend the table if
you add one. Box `name` only matters in one place: a box named `dns*` becomes its team's
resolver (`main.tf`).

Randomising which services/misconfigs land on each box is the default, but not the only
option — you can pin the exact set instead by hand-authoring
`competitions/<id>/box_services.json` / `box_vulns.json` yourself (either file alone is enough
to count as pinned). See [usage-agents.md](usage-agents.md#pinning-a-competitions-configuration)
for the file shapes and the `nakon catalog` validator.

### Windows domain-join boxes

Promoting a box to an AD domain controller (`ADDS`) and joining another box to it (`Domain
Join`) don't go through `box_vulns.json`/`box_services.json` like every other config — a reboot
mid-plan silently kills every step nakon had queued after it (a box's full `configurations` list
runs as one script), so these two need to be the *only* thing in their own deploy pass, and
that pass has to run strictly after every team's boxes exist (promoting a DC before cloning would
clone its live AD database to every other team — Terraform's own comments call this out as an
unsafe-DC-clone scenario). `create-competition.py` handles this separately, driven by a new
per-competition file, `competitions/<id>/domain_roles.json`:
```json
{"dc01": "dc", "member01": "member"}
```
The first `"dc"`-role box (by name) is promoted to a fresh forest per team
(`team<identifier>.local`, so each team's forest is independent, same isolation guarantee as
everything else in this range); every `"member"`-role box in the same team is joined to it
afterward. A competition without this file is unaffected — this is purely additive. See
`deploy_windows_domain_configs()` in `create-competition.py` for the exact sequencing, and
[Adding a template VM → Windows box templates](#windows-box-templates) for the template this
needs.

### Theming usernames

Two account names are themeable per competition, both prompted for (Enter keeps the defaults)
right after the box picker when creating a competition, and always saved to
`competitions/<id>/users.json`:

- **Box login** — the account Terraform's cloud-init creates on every team box clone, and the
  one nakon connects with (its `machines[].user` field — see `generate_nakon_config()`).
  Default `ubuntu`.
- **Credlist accounts** — exactly 3 comma-separated usernames, the accounts Quotient's
  Ssh/Smtp/Imap/Sql/Ftp checks authenticate against (`fix_services_on_boxes()` creates them on
  every box; `push_event_conf()` writes them to Quotient's `linux.credlist`). Default
  `admin,user1,user2`; the first name keeps the elevated MySQL grant the old `admin` account had.

`users.json` looks like:
```json
{"box_username": "engineer", "credlist_usernames": ["svc-admin", "analyst1", "analyst2"]}
```
Absent means both stay at their defaults — every competition created before this feature existed
keeps behaving exactly as it did. See
[usage-agents.md](usage-agents.md#pre-authoring-a-competition) for pre-authoring/pinning it
non-interactively via `--box-username`/`--credlist-usernames`.

### The competitor packet

`python3 generate-packet.py competitions/<id>` renders `competitions/<id>/packet.md` — a
network/system/services briefing (MACCDC-style) you can hand competitors ahead of the event,
before real credentials exist. It needs only `Compfile`/`boxes.json`/`box_services.json` to
exist (no live deploy required), and deliberately never reads `box_vulns.json` — planted
misconfigs stay out of it. See [usage-agents.md](usage-agents.md#generate-packetpy) for details.

## Run it

```bash
python3 create-competition.py
```

It loads `.env`, asks whether to reuse an existing competition or create a new one, asks how
many teams (it names/passwords them for you, see [Configure the event](#configure-the-event)),
generates nakon's machine + vuln list itself (by calling `nakon randomize` from `vendor/nakon`),
then runs `deploy()` — see the [README](../README.md#how-it-works) for what the seven deploy
phases actually do. A few minutes per box; expect most of the time in package installs and
image builds. At the end it prints a summary and writes `competitions/<id>/credentials.txt`
(mode 0600) with the scoreboard URL, admin login, every team's login, and the scoring engine's
SSH command — copy this down, it's the only place team/admin passwords are shown.

For CLI flags that skip these prompts (useful interactively too, e.g. `--yes` or
`--from-phase`), see [usage-agents.md](usage-agents.md).

**Teardown**: `python3 destroy-competition.py`. It destroys the team2+ boxes that were cloned
via the Proxmox API (they're not in Terraform state, so `terraform destroy` alone can't remove
them — it reads `competitions/<id>/cloned_vms.json`), then runs `terraform destroy`. It needs
that competition's `teams.json` + `boxes.json` (both written by the deploy). Templates aren't
touched.

**Add a team mid-event**: don't re-run `create-competition.py` for this. A fresh run
regenerates **every** team's password *and* its phase [1/7] destroys the existing team boxes,
engine, and bridges before rebuilding — a full teardown, not an incremental add. To add a team
to a live event, do it by hand: clone the boxes onto a new `vmbr<identifier>` bridge the way
`clone_team_boxes()` does, give the engine an address on that bridge, and re-push `event.conf`
so Quotient knows about the team.

**Add a box type**: see [Adding a template VM](#adding-a-template-vm) to build/tag the template
— then it just shows up as an option in `create-competition.py`'s box picker. New boxes are
built for team1 and cloned out to every other team.

**Running `terraform` directly** (skipping `create-competition.py`) — note this only builds
team1's boxes, the scoring engine, and the bridges; it does **not** bootstrap Quotient, run
nakon, or clone boxes to the other teams (the driver does all of that after `apply`), so a bare
`terraform apply` leaves you with an unconfigured range. Terraform also doesn't load `.env`
itself, so export it into your shell first. Plain `source .env` breaks on values with
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

## Recovering boxes mid-competition

A deploy is all-or-nothing, and rebuilding the whole range an hour into an event is not an
option. So the deploy leaves two disk-only snapshots on every box:

- **`tz-base`** — taken in phase 5/6 once the box boots with working DNS and an `ubuntu` login,
  before nakon plants anything.
- **`tz-ready`** — taken at the end of phase 6, after nakon and service hardening. This is
  literally the disk the competition started on.

`redeploy-competition.py` rolls a *filtered* set of boxes back to either point. Always look
first:

```bash
python3 redeploy-competition.py --competition <id> --teams 3 --dry-run
```

That prints each selected box with its vmid, IP, and which snapshots it has. Then:

```bash
# team 3's whole set, back to how the competition started (seconds per box)
python3 redeploy-competition.py --competition <id> --teams 3

# just one box
python3 redeploy-competition.py --competition <id> --teams 3 --boxes web01

# team 3's linux boxes, back to tz-base and re-planted by nakon
python3 redeploy-competition.py --competition <id> --teams 3 --platform linux \
    --mode rollback-base
```

`--teams` accepts whatever you have in front of you: `team3`, `3`, or the subnet identifier
`103` off the box's IP.

**A rollback throws away everything that team did to the box.** It's a reset, not a repair —
the tool says so and asks before doing it. If you only want to put a dead service back without
touching the team's work, use `--mode reconfigure`, which re-runs the deploy's configuration
steps against the live box and rolls nothing back. If the VM is gone entirely or won't boot,
`--mode rebuild` recreates it from its Packer template.

Run `python3 verify-competition.py competitions/<id>` afterwards, and give the scoreboard a
round or two to pick the box back up.

Snapshots need a snapshot-capable datastore — ZFS, LVM-thin, Ceph, or qcow2 on file storage.
Thick LVM can't snapshot; on such a host the deploy warns and only `reconfigure`/`rebuild` are
available. See [usage-agents.md](usage-agents.md#redeploy-competitionpy) for the full flag list.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| VM clone lock errors | Proxmox can't clone two VMs at once | Always use `-parallelism=1` |
| Template not found in lookup | Not tagged `template`, or the template name you typed into the box picker doesn't match exactly | Check tag + exact VM name in Proxmox UI |
| Can't SSH into a freshly cloned box | Template's cloud-init is broken/disabled | Verify on a scratch clone (`cloud-init status --long`) before tagging as a template |
| `apt-get install` fails or hangs on a fresh clone | Racing `unattended-upgrades`' first-boot run for the dpkg lock | Already retried automatically (60×2s); rerun apply if it still loses the race |
| Quotient panics on startup re: credlist | A box check references a credlist that was never staged | `create-competition.py` pushes `linux.credlist` alongside `event.conf`, so the two always agree — check `config/credlists/` on the scoring engine if not |
| nakon can't find its config on the scoring engine | Ran a bare `terraform apply` instead of `create-competition.py` — Terraform no longer runs nakon or ships it a machine list; the driver scps it to the engine in phases 5/6 | Run `python3 create-competition.py` (it generates `competitions/<id>/nakon-config.json` and pushes it), rather than driving Terraform by hand |
| nakon: MySQL access denied / connection refused | vulndb unreachable from your laptop when generating the machine list, or `vendor/nakon/.env` creds/db name don't match actual grants | Confirm reachability, check `SHOW GRANTS`, fix `vendor/nakon/.env` |
| nakon can't SSH into a box | Box still booting, or its template's account/password doesn't match what `generate_nakon_config()` assumes | Check `boxes_per_team[].template`'s cloud-init account |
| Bridge creation fails (403) | API token missing `Sys.Modify` | Re-add the permission at the right path/level |
| Everything went down at once, after a scoring-engine reboot | The forwarding/NAT rules didn't get re-applied — Docker rebuilds `FORWARD` (policy `DROP`) on every start | `sudo systemctl status range-firewall.service` on the engine; `sudo systemctl restart range-firewall.service` re-applies them (the script is idempotent). It's ordered after `docker.service` and should do this automatically at boot |
| A team can reach another team's boxes | `range-firewall.sh` isn't applied — the empty bridges don't isolate anything by themselves, since the engine has a NIC on every team bridge and forwards between them | `sudo /usr/local/sbin/range-firewall.sh` on the engine, then check `sudo iptables -L FORWARD -n --line-numbers` for the `192.168.0.0/16 → 192.168.0.0/16 DROP` rule |
| `/tmp` full on a team VM — `No space left on device` in apt output, or `size mismatch in put!` from `deploy.py` | Stale files from a previous failed deploy accumulated in `/tmp` (a RAM-backed tmpfs); `deploy.py` does not clean up after itself | SSH into the scoring engine, then into the affected machine using credentials from `config.json`, and run `find /tmp -maxdepth 1 -type f -delete`; re-run `create-competition.py`. Also fix `deploy.py` in the `nakon` repo to remove `/tmp/<attachment>` after each script run. |
| A box installs no services / DNS fails before nakon | Cloud-init's `dns.servers` is silently ignored on Debian with static IPs, leaving `/etc/resolv.conf` empty. The driver's `fix_dns_on_boxes()` (phases 5 and 6, run over the engine jump host) repairs it before nakon's `apt-get`, retrying up to 8× — nakon would otherwise install nothing there and still report success | Watch the `[5/7]`/`[6/7]` DNS output for the failing box. To debug: SSH through the scoring engine to that box and check `cat /etc/resolv.conf`, `cat /etc/systemd/resolved.conf.d/upstream.conf`, and `getent hosts deb.debian.org`. Re-run `create-competition.py` once fixed |
| Services all show down on the scoreboard | First check round only lands `Delay`+`Jitter` (~70 s) after the summary prints — wait it out first. If they stay down, nakon installed nothing (see the DNS row above), or a login check is authenticating with credentials no box has | On a box: `ss -ltnp` to see whether the service is even listening. If it is, the check is failing auth — compare `/opt/quotient/config/credlists/linux.credlist` on the scoring engine against the box's real accounts. `Sql` checks need a matching *database* user, which nakon's install script has to create |
| Something might be wrong mid-competition (nothing crashed loudly, scoreboard just looks off) | `range-healthcheck.timer` on the engine checks Quotient's container/API and the NAT/isolation rules every 60s and only writes to its log on failure — nothing pushes an alert | `sudo tail -f /var/log/range-healthcheck.log` on the engine during a live event, or `python3 verify-competition.py competitions/<id>` from your workstation (it reports the timer's status + recent log lines as part of its output) |
| One team's boxes are broken mid-event and rebuilding the range isn't an option | That's what the snapshots are for — see [Recovering boxes mid-competition](#recovering-boxes-mid-competition) | `python3 redeploy-competition.py --competition <id> --teams N --dry-run` to see what's affected, then drop `--dry-run` |
| `redeploy-competition.py` says a box has no `tz-ready` snapshot | The range was deployed before snapshotting existed, or `TF_VAR_datastore` is thick LVM (which can't snapshot) | Use `--mode reconfigure` (re-runs config on the live box) or `--mode rebuild` (recreates from template). For future ranges, point `TF_VAR_datastore` at ZFS/LVM-thin/Ceph or a qcow2 dir store |
| Resumed `--from-phase 7` and worried it re-seeded/re-unpaused/re-created injects | Each of phase 7's three sub-steps is gated on its own flag in `competitions/<id>/.deploy_state.json` (`seeded`/`engine_unpaused`/`injects_created`) — a resume skips whatever already succeeded | Check those flags in `.deploy_state.json`; `verify-competition.py`'s INJECTS check will also catch a duplicate-injects regression if the guard ever failed |
