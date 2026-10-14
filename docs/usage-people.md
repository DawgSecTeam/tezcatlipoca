# Usage — people

Interactive operation: one-time setup, templates, env vars, running a deploy. Scripted use: [usage-agents.md](usage-agents.md). Browser front end + run queue: [tezcatlipoca-dashboard](https://github.com/DawgSecTeam/tezcatlipoca-dashboard) (address and credentials: `infrastructure-status.md`, gitignored).

## Prerequisites (per Proxmox host)

- **API token**: user + token with `PVEAdmin` at `/`, or at minimum `VM.Allocate`, `VM.Clone`, `VM.Config.All`, `VM.PowerMgmt`, `Datastore.AllocateSpace`, `Datastore.Audit`, `Sys.Modify`, `SDN.Use`.
- **Templates**: see below. An in-path firewall lineup (`in_path: true`) also needs a `pfsense` template on every node hosting teams ([environment-facts.md](environment-facts.md#templates)) and the seed `competitions/<id>/pfsense/pfsense-config-orig.xml` (copy from `competitions/pfsense-ad/pfsense/`). Preflight refuses without the template; phase 5 refuses without the seed.
- **SSH keypair**: `ssh-keygen -t ed25519 -f ~/.ssh/range_key -C range-automation -N ""`
- **Tooling**: Terraform (HashiCorp apt repo); `pip install toml requests python-dotenv pyyaml urllib3` (`tools/` VNC screenshots also need `websocket-client Pillow cryptography`).
- **Config**: `cp .env.example .env` (gitignored; the only home for real creds).
- **vulndb**: nakon's build/randomize run on your workstation, so vulndb must be reachable from it; the engine needs none. Connection details go in `vendor/nakon/.env` (`host`, `user`, `password`, `database`).

## Templates

Every VM is a clone of a hand-built Proxmox template. The two roles differ:

- **Engine template** (`TF_VAR_template_vm_id`; the base the per-competition engine template is built from): Terraform never touches its cloud-init, so whatever account/key/sudo is baked in is what every clone gets.
- **Box templates** (`boxes_per_team[].template`): Terraform's `initialization.user_account` sets `var.box_username`, `var.ssh_public_key`, and a per-competition `var.box_password` (nakon uses password auth). The template only needs working cloud-init.

| Role | vCPU | RAM | Disk |
|---|---|---|---|
| Engine | 4 | 4 GB | 40 GB (hard-coded in `main.tf`) |
| Box | ≥1 | ≥512 MB | ≥10 GB |

`/tmp` is a tmpfs at 50% of RAM, and nakon stages attachments there. A 512 MB box has ~256 MB, and a large attachment or leftovers from a failed deploy fill it (`No space left on device`, `size mismatch in put!`). Raise `memory_mb` for that box type.

1. **Base VM** from a cloud-init image (or install `cloud-init` yourself). Install `qemu-guest-agent` (required on the engine template: `main.tf` reads its IP back through the agent). Boot once; `cloud-init status --long` must say `done`.
2. **Seed credentials (engine template only)**: an account named exactly `var.vm_username`, with `var.ssh_public_key` in `authorized_keys` and passwordless sudo (`/etc/sudoers.d/90-range-automation`). Confirm both survive a reboot.
3. **Reset cloud-init immediately before shutdown**: `sudo cloud-init clean --logs --seed`. Skipping it is silent: clones look like the same instance rebooting, cloud-init skips network config, and `status` still says `done`.
4. **Convert and tag**: `qm template <vmid>; qm set <vmid> --tags template`. `main.tf` finds box templates by exact name among tagged VMs, and the box picker lists only tagged ones.
5. **Verify** with a scratch full clone. Engine: key SSH works and `sudo -n true` passes. Box: `cloud-init status` is `done` **and** `ip -br addr` shows the `ipconfig0` address. Destroy the clone.
6. **Wire in**: engine → `TF_VAR_template_vm_id` in `.env` (global). Box → nothing; the picker queries tagged templates.

### Non-Ubuntu Linux

- **Alpine**: the official cloudinit qcow2 lacks `bash`, `shadow`, `sudo`, `qemu-guest-agent`, and Alpine cloud-init never creates `/etc/resolv.conf`, so the bootstrap must install those and set `manage_resolv_conf: true` (else `apk` times out on DNS and `packages:` silently no-ops, once per instance). Builder: `competitions/distro-matrix-2026-09-27/build_alpine_ci_template.sh`.
- **Fedora**: the official Cloud Base qcow2 already has cloud-init and the agent. Set `SELINUX=permissive` in `/etc/selinux/config` before sealing: the agent domain is confined, so an enforcing guest can't be golden-built when SSH isn't available (`setenforce 0` and `/etc/ssh` writes are denied). Verify `getenforce` → `Permissive` on a clone.
- Pick `box_username` before building. `setup_ubuntu_auth` assumes it isn't a legacy system account (`operator` is a nologin uid-11 account on Fedora and classic Debian; `medic` is safe).

### VyOS firewall template

`tools/build-vyos-provision-template.py` builds per node from the stream ISO (`download → create → install → provision → check → seal`; run from the worktree root with the node's env). Phase 5 needs: the deploy key in the **config** (`system login user vyos authentication public-keys`; a key written to `authorized_keys` is wiped on reboot), LAN `eth1 = 192.168.1.1/24`, serial getty, and **no `hw-id` lines** (MAC-pinned names rename every fresh-MAC clone's interfaces and kill the config). `check` pushes the real phase-5 script to a scratch and undoes it; `check-key` proves the key path.

### Windows

No cloud-init: `main.tf` skips `initialization` for templates whose name contains `win` (same substring as nakon's `os_to_platform()`), and `bootstrap_windows_box()` sets IP/DNS/Administrator password over the guest agent post-clone.

1. **Unattended install** from an evaluation ISO via `autounattend.xml` on a floppy (`qm set <vmid> --args '-fda /path/autounattend.img'`; a second CD-ROM isn't detected). Declare `xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"` on `<unattend>` if any `wcm:action` is used, or it fails silently past the language screen (which never auto-skips). Use SeaBIOS, SATA disk, `e1000` NIC (no driver injection in WinPE); install virtio-win afterward.
2. **Guest-agent bootstrap**: `msiexec /i <virtio-win>:\virtio-win-gt-x64.msi /qn /norestart` (the agent reports running but is non-functional without the vioserial driver), then `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, start and enable `sshd`, open :22.
3. **Sysprep**: `sysprep /generalize /oobe /shutdown /unattend:<path>` with `<ComputerName>*</ComputerName>`. It fails with 0x3cf2 on current Server images because of Edge; first run `Get-AppxPackage -AllUsers -Name "*MicrosoftEdge*" | Remove-AppxPackage -AllUsers` and confirm it's gone.
4. **Tag** as above. Verify over the guest agent: `$env:COMPUTERNAME` differs from the build name; `sshd` and `QEMU-GA` are `Running` untouched.

## Configure the event

`.env` is the single config for Terraform (`TF_VAR_*`) and the driver; there is no `terraform.tfvars`. Naming: `TF_VAR_<name>` Terraform input; `TEZ_<NAME>` driver knob (new knobs must use it); `NAKON_*`/`VULNDB_*` vendored-tool config. Any other env var the code reads must be in `tests/test_secret_hygiene.py`'s `INTERNAL_ENV` with a reason; that test keeps this table honest.

**Required:**

| Variable | Meaning |
|---|---|
| `TF_VAR_proxmox_endpoint` | API URL, e.g. `https://10.0.0.10:8006/` |
| `TF_VAR_proxmox_api_token` | `user@realm!tokenid=uuid` |
| `TF_VAR_proxmox_node` | Node name |
| `TF_VAR_datastore` | Datastore for engine and clones. Must support snapshots (ZFS, LVM-thin, Ceph, qcow2); thick LVM can't, and rollback modes need them |
| `TF_VAR_template_vm_id` | Engine template vmid |
| `TF_VAR_ssh_public_key`, `TF_VAR_ssh_private_key_path` | Keypair; the path is relative to `terraform/` unless absolute |
| `TF_VAR_vm_username` | Account baked into the engine template; unrelated to box logins |
| `TF_VAR_box_username`, `TF_VAR_box_password`, `TF_VAR_event_name`, `TF_VAR_teams`, `TF_VAR_boxes_per_team` | **Driver-written** from the competition dir (`users.json`, `credentials.txt`, `Compfile`, …). Stale values in an old env are overridden at terraform time. `teams` is `{"team1":{"identifier":"101","password":"…"}}`; `boxes_per_team` max 10, and a `disk_gb` below the template's fails the clone (Proxmox can't shrink) |

**Optional:**

| Variable | Meaning |
|---|---|
| `TF_VAR_engine_mgmt_ip` / `_gw` | Static engine management IP (default `10.0.0.250` / `10.0.0.1`); empty IP = DHCP |
| `TF_VAR_mgmt_bridge` | Management bridge the engine's and each jump VM's mgmt NIC attach to (default `vmbr0`); a `nodes.json` record's `mgmt_bridge` overrides it ([multi-node.md](multi-node.md)) |
| `TF_VAR_scoring_vm_id` | Engine vmid (default 1000); also `--scoring-vmid` |
| `TF_VAR_team_identifiers` | Pin team subnets, e.g. `120,121,122`. Set when node vmids collide with the default 101+ blocks |
| `TEZ_THIN_HEADROOM` | Fraction `(0, 1]` of provisioned disk the headroom preflight counts on thin pools. Unset = strict ([environment-facts.md](environment-facts.md#storage)) |
| `TEZ_RED_SEGMENT` | Routed-red segment matching bad-auto's `deploy.red_subnet` (default `10.200.0.0/24`). Set → satellite jump VMs accept red and SNAT to `192.168.<id>.1`; malformed is a hard error ([multi-node.md](multi-node.md#routed-red)) |
| `TEZ_PORTAL_TUNNEL_TOKEN`, `TEZ_PORTAL_HOSTNAME` | Student portal Cloudflare tunnel. **Event env only**: a practice worktree carrying them claims the public name ([portal.md](portal.md)) |
| `TEZ_PORTAL_OPEN` | `1` opens team console access at start (practice). Default closed |
| `PROXMOX_CA_BUNDLE` / `PROXMOX_TLS_FINGERPRINT` | Pin API TLS; at most one |
| `TF_VAR_proxmox_api_token_<node>` | Per-host token referenced by name from `nodes.json` ([multi-node.md](multi-node.md)) |

Not in `.env`: the Quotient `admin` and `scoring` passwords and the inject/postgres/redis passwords, minted per run into `credentials.txt`. `scoring` is a second admin for automation (unattended verify, harnesses) so scheduled logins don't evict an operator's `admin` session. Boxes are chosen per event: the picker queries tagged templates and saves `boxes.json`, which a reuse replays.

Teams are only counted, not picked: `collect_teams()` names them `team1…`, identifiers `101…`, with fresh passwords every run.

Box `name` doesn't decide scoring; the services picked do. `build_event_conf()` maps each through `_SERVICE_TO_CHECK` in `quotient/setup.py` (apache/nginx → Web, bind → Dns, sshd → Ssh, …). An unmapped service gets no check and a warning; extend the table. A box named `dns*` becomes its team's resolver (`main.tf`).

To pin services/misconfigs: [usage-agents.md](usage-agents.md#pinning).

### Domain-join boxes

`ADDS` and `Domain Join` reboot, which kills every queued step in a plan, so each is the only config in its own pass and runs after all teams' boxes exist. They are driven by `competitions/<id>/domain_roles.json` (`{"dc01": "dc", "member01": "member"}`): the first `dc` box per team becomes forest `team<identifier>.local`; every `member` (Windows or Linux) joins it. Absent file = no domains. Sequencing: `deploy_domain_configs()` in `guests/domain_ops.py`; template: [Windows](#windows).

### Usernames

Prompted after the box picker, saved to `users.json` (absent = defaults):

- **Box login**: the account cloud-init creates and nakon uses. Default `ubuntu`.
- **Credlist accounts**: exactly 3, the accounts Quotient's Ssh/Smtp/Imap/Sql/Ftp checks use (created by `fix_services_on_boxes()`, written to `linux.credlist`). Default `admin,user1,user2`; the first keeps the elevated MySQL grant.

```json
{"box_username": "engineer", "credlist_usernames": ["svc-admin", "analyst1", "analyst2"]}
```

### Competitor packet

`python3 generate-packet.py competitions/<id>` ([usage-agents.md](usage-agents.md#generate-packetpy)).

## Run it

`python3 create-competition.py` asks reuse-or-new, team count, generates the nakon list (`nakon randomize`), then runs the phases ([architecture.md](architecture.md#phases)). Credentials land in `competitions/<id>/credentials.txt` (0600): scoreboard URL, admin, every team login, engine SSH. It's the only place passwords are shown.

**Teardown**: `python3 destroy-competition.py --competition <id>` ([usage-agents.md](usage-agents.md#destroy-competitionpy)).

**Add a team mid-event**: don't rerun `create-competition.py`; it regenerates every password and destroys the range. Do it by hand: new `vmbr<identifier>` bridge, the team's boxes as Terraform-managed linked clones of the goldens, an engine address on that bridge, re-push `event.conf`.

**Add a box type**: build and tag the template; it appears in the picker. One golden per type; apply #2 clones every team's copy.

**Running `terraform` directly**: a bare `apply` yields an engine, bridges and NICs only. Team boxes need `build_team_boxes` and `golden_template_ids` (phase 4), and it never bootstraps Quotient, runs nakon, or seeds. Terraform doesn't read `.env`, and `source .env` breaks on spaces, `!`, and JSON braces; load it through python-dotenv:

```bash
cd terraform
eval "$(python3 -c "
from dotenv import dotenv_values
for k, v in dotenv_values('../.env').items():
    print(f'export {k}={v!r}')
")"
terraform init && terraform apply -parallelism=1
```

## Recovering boxes mid-event

Every box has two disk-only snapshots: `tz-base` (after boot, DNS and login, before post-clone planting; end of phase 4) and `tz-ready` (end of phase 7, as delivered). Modes: [usage-agents.md](usage-agents.md#redeploy-modes).

```bash
python3 redeploy-competition.py --competition <id> --teams 3 --dry-run   # always first
python3 redeploy-competition.py --competition <id> --teams 3 --boxes web01 --mode reset
```

`--teams` takes `team3`, `3`, or the subnet identifier `103`. Rollbacks discard the team's changes; `reconfigure` restores a dead service without touching their work. `--dry-run` also shows each box's starting rung and PVE tags (a stale `run-<id>` is worth knowing before anything is destroyed). Afterward run `verify-competition.py` and allow a round or two. Mid-event details and caveats: a planted Linux box that is restarted may deny all SSH at the PAM account stage ([known-issues.md](known-issues.md)); `reset` detects and escalates past it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Clone lock errors | Parallel clones; always `-parallelism=1` |
| Template not found | Not tagged `template`, or name mismatch |
| Can't SSH a fresh clone | Broken cloud-init; verify on a scratch clone before tagging |
| `apt-get` fails on a fresh clone | Racing `unattended-upgrades` for the dpkg lock; retried 5×15 s, rerun apply if it loses |
| Quotient panics on credlist | A check references a credlist that wasn't staged; `linux.credlist` is pushed with `event.conf`, check `config/credlists/` on the engine |
| nakon has no config on the engine | Bare `terraform apply`; the driver scps stage configs in phases 4–7 |
| nakon MySQL denied/refused | vulndb unreachable from the workstation, or `vendor/nakon/.env` wrong; check `SHOW GRANTS` |
| nakon can't SSH a box | Still booting, or the template account doesn't match `generate_nakon_config()` |
| Bridge creation 403 | Token lacks `Sys.Modify` |
| Everything down after an engine reboot | Docker rebuilt `FORWARD`; `sudo systemctl restart range-firewall.service` (idempotent, normally automatic after `docker.service`) |
| Teams reach each other | `range-firewall.sh` not applied; run `sudo /usr/local/sbin/range-firewall.sh`, check `iptables -L FORWARD -n --line-numbers` for the `192.168.0.0/16 → 192.168.0.0/16 DROP` |
| `/tmp` full on a box | Leftovers from a failed deploy; `find /tmp -maxdepth 1 -type f -delete` via the engine. `deploy.py` should remove attachments after each run |
| No services / DNS fails before nakon | Cloud-init `dns.servers` is ignored on Debian with static IPs; `fix_dns_on_boxes()` repairs it (8× retries). Check `/etc/resolv.conf`, `/etc/systemd/resolved.conf.d/upstream.conf`, `getent hosts deb.debian.org` |
| Services all down | The first round lands `Delay`+`Jitter` (~70 s) after the summary. Otherwise `ss -ltnp` on the box; if listening, compare `/opt/quotient/config/credlists/linux.credlist` with the box's accounts (`Sql` needs a matching database user) |
| Scoreboard looks off, nothing crashed | `range-healthcheck.timer` logs failures only: `sudo tail -f /var/log/range-healthcheck.log`, or `verify-competition.py` |
| No `tz-ready` snapshot | Thick LVM, or deployed before snapshots; use `reconfigure` or `rebuild` |
| `--from-phase 7` worry about reseeding | Phase 8 steps are flag-gated in `.deploy_state.json`; the INJECTS gate catches duplicates |
