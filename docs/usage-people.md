# Usage — people

Interactive operation: one-time setup, templates, env vars, running a deploy. Scripted use: [usage-agents.md](usage-agents.md). Browser front end + run queue: [tezcatlipoca-dashboard](https://github.com/DawgSecTeam/tezcatlipoca-dashboard) (address and credentials: `infrastructure-status.md`, gitignored).

## Prerequisites (per Proxmox host)

- **API token**: user + token with `PVEAdmin` at `/`, or at minimum `VM.Allocate`, `VM.Clone`, `VM.Config.All`, `VM.PowerMgmt`, `Datastore.AllocateSpace`, `Datastore.Audit`, `Sys.Modify`, `SDN.Use`.
- **Templates**: see below. An in-path firewall lineup (`in_path: true`) also needs a `pfsense` template on every node hosting teams ([environment-facts.md](environment-facts.md#templates)) and the seed `competitions/<id>/pfsense/pfsense-config-orig.xml` (copy one from the `pfsense-ad` competition under the comps root — `knobs.competitions_root`/`TEZ_COMPETITIONS_ROOT`, else `./competitions`, else the sibling `../tezcatlipoca-competitions`). Preflight refuses without the template; phase 5 refuses without the seed.
- **SSH keypair**: `ssh-keygen -t ed25519 -f ~/.ssh/range_key -C range-automation -N ""`
- **Tooling**: `pip install toml requests python-dotenv pyyaml urllib3` (`tools/` VNC screenshots also need `websocket-client Pillow cryptography`).
- **Config**: `cp tezcatlipoca.example.toml tezcatlipoca.toml`; `tezcatlipoca.toml` is gitignored (secrets stay in the environment; see [Configure the event](#configure-the-event)).
- **vulndb**: nakon's build/randomize run on your workstation, so vulndb must be reachable from it; the engine needs none. Connection details go in `vendor/nakon/.env` (`host`, `user`, `password`, `database`).

## Templates

Every VM is a clone of a hand-built Proxmox template. The two roles differ:

- **Engine template** (`proxmox.template_vm_id`; the base the per-competition engine template is built from): its cloud-init is never rewritten, so whatever account/key/sudo is baked in is what every clone gets.
- **Box templates** (`boxes.json` `template`): the clone sets `ciuser`/`cipassword` from the competition's box login and `sshkeys` from `ssh.public_key` (nakon uses password auth). The template only needs working cloud-init.

| Role | vCPU | RAM | Disk |
|---|---|---|---|
| Engine | 4 | 4 GB | 40 GB (`ENGINE_DISK_GB` in `estate/apply.py`) |
| Box | ≥1 | ≥512 MB | ≥10 GB |

`/tmp` is a tmpfs at 50% of RAM, and nakon stages attachments there. A 512 MB box has ~256 MB, and a large attachment or leftovers from a failed deploy fill it (`No space left on device`, `size mismatch in put!`). Raise `memory_mb` for that box type.

1. **Base VM** from a cloud-init image (or install `cloud-init` yourself). Install `qemu-guest-agent` (required on the engine template: the deploy reads its IP back through the agent). Boot once; `cloud-init status --long` must say `done`.
2. **Seed credentials (engine template only)**: an account named exactly `engine.username` (default `sysadmin`), with `ssh.public_key` in `authorized_keys` and passwordless sudo (`/etc/sudoers.d/90-range-automation`). Confirm both survive a reboot.
3. **Reset cloud-init immediately before shutdown**: `sudo cloud-init clean --logs --seed`. Skipping it is silent: clones look like the same instance rebooting, cloud-init skips network config, and `status` still says `done`.
4. **Convert and tag**: `qm template <vmid>; qm set <vmid> --tags template`. The deploy finds box templates by exact name among tagged VMs (`infra/resources.py`), and the box picker lists only tagged ones.
5. **Verify** with a scratch full clone. Engine: key SSH works and `sudo -n true` passes. Box: `cloud-init status` is `done` **and** `ip -br addr` shows the `ipconfig0` address. Destroy the clone.
6. **Wire in**: engine → `template_vm_id` under `[proxmox]` in `tezcatlipoca.toml` (global). Box → nothing; the picker queries tagged templates.

### Non-Ubuntu Linux

- **Alpine**: the official cloudinit qcow2 lacks `bash`, `shadow`, `sudo`, `qemu-guest-agent`, and Alpine cloud-init never creates `/etc/resolv.conf`, so the template build must install those and set `manage_resolv_conf: true` (else `apk` times out on DNS and `packages:` silently no-ops, once per instance).
- **Fedora**: the official Cloud Base qcow2 already has cloud-init and the agent. Set `SELINUX=permissive` in `/etc/selinux/config` before sealing: the agent domain is confined, so an enforcing guest can't be golden-built when SSH isn't available (`setenforce 0` and `/etc/ssh` writes are denied). Verify `getenforce` → `Permissive` on a clone.
- Pick `box_username` before building. `setup_ubuntu_auth` assumes it isn't a legacy system account (`operator` is a nologin uid-11 account on Fedora and classic Debian; `medic` is safe).

### VyOS firewall template

`tools/build-vyos-provision-template.py` builds per node from the stream ISO (`download → create → install → provision → check → seal`; run from the worktree root with the node's env). Phase 5 needs: the deploy key in the **config** (`system login user vyos authentication public-keys`; a key written to `authorized_keys` is wiped on reboot), LAN `eth1 = 192.168.1.1/24`, serial getty, and **no `hw-id` lines** (MAC-pinned names rename every fresh-MAC clone's interfaces and kill the config). `check` pushes the real phase-5 script to a scratch and undoes it; `check-key` proves the key path.

### Windows

No cloud-init: the clone skips cloud-init config for templates whose name contains `win` (same substring as nakon's `os_to_platform()`), and `bootstrap_windows_box()` sets IP/DNS/Administrator password over the guest agent post-clone.

1. **Unattended install** from an evaluation ISO via `autounattend.xml` on a floppy (`qm set <vmid> --args '-fda /path/autounattend.img'`; a second CD-ROM isn't detected). Declare `xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"` on `<unattend>` if any `wcm:action` is used, or it fails silently past the language screen (which never auto-skips). Use SeaBIOS, SATA disk, `e1000` NIC (no driver injection in WinPE); install virtio-win afterward.
2. **Guest-agent bootstrap**: `msiexec /i <virtio-win>:\virtio-win-gt-x64.msi /qn /norestart` (the agent reports running but is non-functional without the vioserial driver), then `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, start and enable `sshd`, open :22.
3. **Sysprep**: `sysprep /generalize /oobe /shutdown /unattend:<path>` with `<ComputerName>*</ComputerName>`. It fails with 0x3cf2 on current Server images because of Edge; first run `Get-AppxPackage -AllUsers -Name "*MicrosoftEdge*" | Remove-AppxPackage -AllUsers` and confirm it's gone.
4. **Tag** as above. Verify over the guest agent: `$env:COMPUTERNAME` differs from the build name; `sshd` and `QEMU-GA` are `Running` untouched.

## Configure the event

The deployment surface is **`tezcatlipoca.toml`** at the repo root, copied from
`tezcatlipoca.example.toml` and gitignored. `estate/config.py` reads it as one `Config` object
with typed sections — `proxmox`, `ssh`, `engine`, `headscale`, `portal`, `knobs` — and holds no
secrets.

Every key also accepts an env override, canonically `TEZ_` + the key upper-cased with dots as
underscores: `proxmox.endpoint` → `TEZ_PROXMOX_ENDPOINT`. Keys under `knobs` drop the prefix
(`knobs.thin_headroom` → `TEZ_THIN_HEADROOM`). The environment beats the file, because the
run-queue dashboard injects per-run values (node tokens, the engine mgmt IP) into the process env
and those must win over a committed profile. `TEZ_CONFIG` points the whole read at another file.

A token is **named, not stored**: put its value in the environment and reference the variable name
(the same indirection `nodes.json` uses for per-host tokens). `.env` is still loaded by the
entrypoints (python-dotenv: `deploy.py`, `destroy-competition.py`, `redeploy-competition.py`,
`verify-competition.py`, `run-agent-scrim.py`, `competition-ops.py`) as a plain dotenv layer of
canonical `TEZ_*` names, so it is one more source for a key's value and it beats the profile. Every
accepted env name is derived from `estate/config.py`'s `_SPEC` (`TEZ_` + the key upper-cased, dots as
underscores); `NAKON_*`/`VULNDB_*` are vendored-tool config. Any other env var the code reads must
be in `tests/hygiene/test_secret_hygiene.py`'s `INTERNAL_ENV` with a reason; that test keeps this table
honest. The dashboard/portal handoff variables (`TEZ_DASHBOARD_*`, `TEZ_OPS_*`, `PORTAL_*`) are
`INTERNAL_ENV`, not part of this operator surface.

**Required** (the first five have no usable default; `node`/`datastore` default only to placeholders):

| Key | Env override | Meaning |
|---|---|---|
| `proxmox.endpoint` | `TEZ_PROXMOX_ENDPOINT` | API URL, e.g. `https://10.0.0.10:8006/` |
| `proxmox.api_token` | `TEZ_PROXMOX_API_TOKEN` | `user@realm!tokenid=uuid`; set it in the environment, never in the profile |
| `proxmox.template_vm_id` | `TEZ_PROXMOX_TEMPLATE_VM_ID` | Engine template vmid |
| `ssh.public_key`, `ssh.private_key_path` | `TEZ_SSH_PUBLIC_KEY`, `TEZ_SSH_PRIVATE_KEY_PATH` | Keypair. A relative path resolves against the checkout root |
| `proxmox.node` | `TEZ_PROXMOX_NODE` | Node name (default `pve` — set it per host) |
| `proxmox.datastore` | `TEZ_PROXMOX_DATASTORE` | Datastore for engine and clones (default `local-lvm`). Must support snapshots (ZFS, LVM-thin, Ceph, qcow2); thick LVM can't, and rollback modes need them |

**Optional:**

| Key | Env override | Meaning |
|---|---|---|
| `engine.username` | `TEZ_ENGINE_USERNAME` | Account baked into the engine template (default `sysadmin`); unrelated to box logins |
| `engine.mgmt_ip` / `engine.mgmt_gw` | `TEZ_ENGINE_MGMT_IP` / `TEZ_ENGINE_MGMT_GW` | Static engine management IP (default `10.0.0.250` / `10.0.0.1`); empty IP = DHCP |
| `proxmox.tls_fingerprint` / `proxmox.ca_bundle` | `TEZ_PROXMOX_TLS_FINGERPRINT` / `TEZ_PROXMOX_CA_BUNDLE` | Pin API TLS; at most one |
| `headscale.url`, `.ssh_host`, `.ssh_user`, `.ssh_password`, `.infra_user`, `.key_ttl` | `TEZ_HEADSCALE_*` | Participant access: required unless the Compfile opts out with `remote_access 0`; `key_ttl` (`TEZ_HEADSCALE_KEY_TTL`) defaults `72h` ([remote-access.md](remote-access.md)) |
| `portal.hostname`, `.tunnel_token`, `.open` | `TEZ_PORTAL_HOSTNAME`, `TEZ_PORTAL_TUNNEL_TOKEN`, `TEZ_PORTAL_OPEN` | Student portal. **Event profile only**: one carrying the tunnel pair claims the public name ([portal.md](portal.md)) |
| `knobs.thin_headroom` | `TEZ_THIN_HEADROOM` | Fraction `(0, 1]` of provisioned disk the headroom preflight counts on thin pools. Unset = strict |
| `knobs.gaia_path` | `TEZ_GAIA_PATH` | Gaia checkout on the engine host (default `~/gaia`); the Mythic C2 setup runs from it |
| `knobs.cycle_timeout`, `knobs.python`, `knobs.competitions_root`, `knobs.artifacts_archive`, … | `TEZ_*` | The rest of the tuning knobs; the full key set is `estate/config.py`'s `_SPEC`, the common ones are commented in `tezcatlipoca.example.toml` |
| `nodes.json` `token_env` | that host's own variable | Per-host API token referenced by name ([multi-node.md](multi-node.md)) |

**Per-run identity:** `knobs.scoring_vmid` (engine vmid, default 1000) and
`knobs.team_identifiers` (pin team subnets, e.g. `120,121,122`). The matching flags
(`--scoring-vmid`, `--team-identifiers`) win over both, and their env forms (`TEZ_SCORING_VMID`,
`TEZ_TEAM_IDENTIFIERS`) beat the profile. Set the latter when the host's live vmids collide with the
default 101+ blocks.

**Not config, generated per competition:** the team roster with its passwords, the box list, the
box login, and the event name are read or minted from the competition dir (`collect_teams()`,
`users.json`, the box picker) and written into the state document, `teams.json` and
`credentials.txt` — a stale value elsewhere cannot override them. The box list caps at 10 types
(`MAX_BOXES_PER_TEAM`); a `disk_gb` below the template's fails the clone (Proxmox can't shrink).

Not in any config file: the Quotient `admin` and `scoring` passwords and the inject/postgres/redis passwords, minted per run into `credentials.txt`. `scoring` is a second admin for automation (unattended verify, harnesses) so scheduled logins don't evict an operator's `admin` session. Boxes are chosen per event: the picker queries tagged templates and saves `boxes.json`, which a reuse replays.

Teams are only counted, not picked: `collect_teams()` names them `team1…`, identifiers `101…`, with fresh passwords every run.

Box `name` doesn't decide scoring; the services picked do. `build_event_conf()` maps each through `_SERVICE_TO_CHECK` in `quotient/setup.py` (apache/nginx → Web, bind → Dns, sshd → Ssh, …). An unmapped service gets no check and a warning; extend the table.

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

**Add a team mid-event**: don't rerun `create-competition.py`; it regenerates every password and destroys the range. Do it by hand: new `vmbr<identifier>` bridge, the team's boxes as linked clones of the goldens (the estate component's create path), an engine address on that bridge, re-push `event.conf`.

**Add a box type**: build and tag the template; it appears in the picker. One golden per type; the phase-4 box build clones every team's copy.

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
| Phase 7 errors `team_beacons is not supported` | An older Compfile still sets `team_beacons`; remove the line — tlaloc plants no team beacons |
| Clone lock errors | The component clones serially; a concurrent cloner on the same datastore (e.g. the workshop portal provisioner) is the usual cause |
| Template not found | Not tagged `template`, or name mismatch |
| Can't SSH a fresh clone | Broken cloud-init; verify on a scratch clone before tagging |
| `apt-get` fails on a fresh clone | Racing `unattended-upgrades` for the dpkg lock; retried 5×15 s, rerun the phase if it loses |
| Quotient panics on credlist | A check references a credlist that wasn't staged; `linux.credlist` is pushed with `event.conf`, check `config/credlists/` on the engine |
| nakon has no config on the engine | The driver scps stage configs in phases 4–7; resume at the phase that stages them |
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
| `--from-phase 7` worry about reseeding | Phase 8 steps are flag-gated in `estate-state.json`; the INJECTS gate catches duplicates |
