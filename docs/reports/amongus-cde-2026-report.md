# amongus-cde-2026 — run report

CDE "Among Us" 4-box competition (mira / skeld / airship / polus), implemented on the
tezcatlipoca pipeline. Worktree: `tezcatlipoca-amongus`, branch `amongus-cde-2026`.

## Status

| Workstream | State |
|---|---|
| Competition definition (`competitions/amongus-cde-2026/`) | DONE — catalog check 0/0, generate dry-run validated (6c3a03a) |
| Pipeline code (check mappings, stage routing, cross-box vars) | DONE — 133/133 tests (15784fa) |
| vulndb catalog rows | DONE — `ad-color-fleet-win` (298), `ad-dns-localhost` (299), `ftp-content-win` (300) |
| `base-centos8-fix` template | DONE — sealed vmid 1032, scratch-validated (password SSH, dnf httpd) |
| `base-ubuntu20.04-fix` template | DONE — sealed vmid 1033, scratch-validated (password SSH) |
| `base-windows-server-2019` template | IN PROGRESS — vmid 1034 |
| 2-team deploy + verify + bad-auto + freeze | PENDING |

## Spec → pipeline mapping

| Box (.octet) | Template | Scored pins | Misconfigs |
|---|---|---|---|
| mira (.2, DC) | base-windows-server-2019 | AD-DNS (Dns 53), C$ share (Tcp 445), WinRM (Tcp 5985), ADDS (Tcp 389, domain_ops) | SMBv1, complexity-off policy, guest admin, 11-color Domain Admin fleet |
| skeld (.3) | base-windows-server 2022 | IIS FTP (Tcp 21), C$ share (Tcp 445) | anonymous FTP drop w/ themed content, FTP firewall rule |
| airship (.4) | base-centos8-fix | Apache (Web 80), SSH (Ssh 22) | vault-repos, selinux-permissive, airship-webapp (real binary) w/ airship:airship DB creds |
| polus (.5) | base-ubuntu20.04-fix | MariaDB (Sql 3306), SSH (Ssh 22) | remote bind, airship@% db user, weak local user |

Domain: `team<id>.local` (user decision — spec's `mira-01.corp.sus` theme kept in the
scenario text). Box login `blackteam`, credlist `red`/`blue`/`imposter`.

## Deviations from the CDE spec (deliberate)

- Domain name is per-team `team<id>.local`, not `mira-01.corp.sus` — the pipeline
  generates per-team forests and verify gates that name; a rename is future work.
- Account passwords come from the per-competition `credentials.txt` rotation, not the
  spec's fixed values (`TOcpACdpCPAt`, `Red123!` …) — the no-default-creds verify gate
  would fail the spec's literals. The AD fleet structure (blackteam + ten colors, all
  Domain Admins, guest elevated) matches the spec exactly.
- skeld's IIS FTP scores as a Tcp port-open check: Quotient's credlist-backed Ftp check
  only has Linux credlist accounts provisioned, so an authenticated Windows FTP check
  would score DOWN forever.
- Windows boxes are administered as `Administrator` (harness convention) — the spec's
  `blackteam` AD account is planted on the domain and usable interactively.

## Template builds (cyberfield .193, node `pve`)

### What the node had

Only `base-windows-server` (2022, vmid 1008), `base-ubuntu24.04-fix` (1007), plus
alpine/fedora/debian variants. `base-ubuntu20.04` (vmid 1001) exists but is a dead
shell — no guest agent, no cloud-init drive, no tags — full-clone probe confirmed it
never gets an IP; do not reuse. Server 2019 / CentOS 8 / focal images were absent.

### Recipe that worked (all three Linux/Windows builds)

1. Server-side fetch: `POST /nodes/pve/storage/local/download-url` with
   `content=import` for cloud images (CentOS 8.4 GenericCloud from cloud.centos.org —
   the 8.5 image is gone upstream; focal-server-cloudimg from cloud-images.ubuntu.com),
   `content=iso` for the eval ISO (the `go.microsoft.com/fwlink` redirect works).
2. Create the build VM in one shot with
   `scsi0: hdrives-zfs:0,import-from=local:import/<image.qcow2>` + `ide2` cloudinit
   drive + `ciuser`/`sshkeys`/static `ipconfig0` on vmbr0, then `resize` the root disk.
3. Boot, wait-agent, SSH in with the automation key, apply the fix pass, then
   `cloud-init clean --logs --seed`, shutdown, rename, `qm template`, tag
   `cloud-init;general;template`.

### Traps hit (all fixed)

- **`sshkeys` form-encoding**: the `+` in the ed25519 key body corrupts
  `application/x-www-form-urlencoded` POSTs → percent-encode the key (`quote()`).
- **CentOS 8 OpenSSH 8.0 has no `Include` directive**: `sshd_config.d/*.conf` drop-ins
  are dead paper (sshd -t fails "Bad configuration option: Include"). Set
  `PasswordAuthentication yes` directly in `/etc/ssh/sshd_config`, and flip
  `ssh_pwauth: 1` in `/etc/cloud/cloud.cfg` (alpine known-issue applies: cloud-init
  re-applies it per-instance).
- **CentOS 8 is EOL**: GenericCloud repos point at the decommissioned
  mirrorlist.centos.org. The `centos-vault-repos` catalog row's sed repoints to
  vault.centos.org; the template additionally pins `/etc/dnf/vars/releasever` to
  `8.4.2105` (vault has no `/8/` alias, so an unpinned `$releasever` 404s).
- **el8 httpd welcome page is a 403**, not 200 — an `apache`-pinned Web check
  (`Path /, Status 200`) only passes once real content answers. On airship the
  `airship-webapp` reverse proxy satisfies it.
- **API-token limits on PVE 9**: cannot set `args` (floppy attach), `file0` rejected,
  `import-from` with arbitrary paths refused, `local:iso` volumes rejected by
  `import-from` (needs `content=import` storage volumes).
- **Windows unattended install**: `autounattend.xml` on a second CD-ROM was not
  auto-detected (2019, same as the 2022 note in usage-people.md) and `xmlns:wcm` must
  be declared on the ROOT `<unattend>` element or setup silently stalls at the WinPE
  language screen (VM idles at ~770MB/~1-4% CPU). Working approach: graft the xml into
  the boot ISO with `xorriso -indev <iso> -outdev <combined> -map autounattend.xml
  /autounattend.xml -boot_image any replay`, plus `<Product><Key></Key></Product>`
  (empty) for eval media.

## Pipeline changes on this branch

- `quotient/setup.py`: `IIS FTP` → Tcp 21 (port-open; no Windows credlist path),
  `ad-dns-localhost` → Dns 53 (localhost A-record, same shape as bind/named).
- `constants.py`: AD-dependent DC configs (`ad-color-fleet-win`, `ad-dns-localhost`,
  `guest-enabled-win`, `Elevate Guest Account`, `weak-password-policy-win`) route to
  the FINAL stage (the unbooted DC golden means its otherwise-golden configs plant
  pre-promotion; these need the domain to exist). `airship-webapp` in REPAIR_STAGE.
- `REQUIRED_VARS` gains the `ip:<box>` cross-box var kind: filled with the same team's
  copy of the target box's IP; generate fails loudly if the target machine is absent.
  `airship-webapp = {DB_USER, DB_PASS literal; DB_HOST ip:polus}`.
- `nakon_ops.py`: `_cross_box_ip()` + `_is_identity_kind()`; stage-fill honors both
  identity kinds; golden-ban covers cross-box.
- `tests/test_amongus_cde.py`: 7 tests over mappings, scoreboard names, per-team
  fills, missing-target failure, final-stage routing.

## vulndb catalog additions

- `ad-color-fleet-win` — blackteam + Red/Blue/Green/Pink/Orange/Yellow/Black/White/
  Purple/Brown as Domain Admins (idempotent password reset on re-run).
- `ad-dns-localhost` — localhost→127.0.0.1 A record in the AD zone (Dns check target).
- `ftp-content-win` — TASKS.txt / CREW_MANIFEST.csv / INJECT_DROP_README.txt seeded
  into the IIS FTP root + anonymous-read authorization (deps: IIS FTP).

## Deploy + validation (2026-09-30 → 10-01)

2 teams (identifiers 113/114), engine vmid 1500 at 10.0.0.251, goldens 1650–1653,
team vmids 1330s/1340s. Final `verify-competition.py --strict-services`: **ALL GATES
PASS** — logins, no_default_creds, services 9/9 UP ×2 teams, pins_registered (9),
isolation, misconfig + survival, injects (2), plant integrity 0 failed, domains
(unique DomainSIDs ×2 forests, all four members joined), plant coverage.

**Reboot test (CDE checklist):** all 8 team boxes rebooted (DCs staggered last);
9/9 services UP on both teams afterwards — services return on their own.

### Live-found fixes during the deploy (all committed + pushed on the branch)

- `template_ops.build_engine_template`: retry the post-clone config PUT through PVE's
  transient-400 window; 900s SSH budget (300s lost twice to first-boot under the
  parallel deploy's node load).
- `tools/engine_finish.py` *(since deleted as dead code)*: finished + converted the
  engine template on an already-up build VM (recovery path when a resume would
  destroy a working clone); wrote the hash to BOTH `.deploy_state.json` and
  `.template-hashes.json` (the reuse check reads the latter). Modern equivalent:
  `redeploy-competition.py --mode engine-recovery`.
- `windows_ops.bootstrap_windows_box`: hard-cycle once (fresh full budget) when the
  sysprep first-boot wait expires — golden-skeld deadlocked pre-specialize on every
  snapshot-rollback boot; the forced stop/start cleared it. Also a fresh exec-phase
  budget (the setup waits consume the entry deadline).
- `deploy._record_coverage`: clear a machine's stale failures when its stage replants
  clean — SMB v1 stayed "failed" across three green replants otherwise.
- `nakon_ops.generate_nakon_config`: fill REQUIRED_VARS identity vars in the BASE
  machine list (second pass) — the bundle lint rejects a script referencing a var the
  base list doesn't declare, even when every stage file carries it.
- `verify-competition.check_domains`: gateway-SSH fallback for the Linux realm probe —
  the PVE agent channel has a per-instance exec breaker that stayed tripped on both
  airships after the deploy-time join-probe failures.
- vulndb rows fixed live: `ftp-content-win` (WebAdministration needs the -Location
  form / applicationHost XML for fresh FTP sites; appcmd exits 13), `SMB v1` (SMB1 is
  a removable FEATURE on 2019/2022 — install it, then the registry flag; the WMI
  provider is a terminating error until the pending restart), `airship-webapp`
  (systemctl stop before cp — ETXTBSY over a running binary), `domain-join` (nmcli
  branch must never be fatal under set -e — fall back to the resolv.conf insert).

### Known gaps / deferred

- **airship domain join via the pipeline**: the fixed `domain-join` row joins cleanly
  now (verified in the phase-6 replay), but the airships were joined by hand first —
  their PVE agent-exec breakers stayed tripped, so verify probes them over gateway SSH.
- **bad-auto coverage**: deferred — this lineup has no red box; red runs coverage from
  its own box after bring-up (`python3 -m badauto coverage` derives rows from
  box_services.json; Windows effects needed: FTPSVC stop/start for skeld-ftp, DNS
  service for mira-dns).
- **inject T0**: offsets anchored at deploy time; one inject is already past close —
  re-anchor at event start via run-agent-scrim's `reanchor_injects()` (UpdateInject
  multipart: re-list every attachment under keep-files or they are deleted).
- **Windows FTP scoring is port-open only** (Tcp 21) — an authenticated Ftp check has
  no Windows credlist provisioning.

## Red-team handoff

- Freeze state: `.frozen.json` (mode 0600) — unfreeze needs `--confirm-unfreeze`,
  pre-competition only.
- Adding misconfigs/malware post-freeze: unfreeze → author catalog rows → pin them in
  `box_vulns.json` → `redeploy-competition.py --mode reconfigure` (keeps teams up).
- The inject drop is skeld's anonymous-read FTP root (`C:\inetpub\ftproot\MyFtpSite`);
  submissions land there per the inject briefings.
- Credentials: `competitions/amongus-cde-2026/credentials.txt` (0600, gitignored).
  AD fleet passwords are the spec's (Red123! …), blackteam = TOcpACdpCPAt; everything
  else is per-competition generated.
- Event day: re-anchor injects at T0, then `POST /api/competition/start` + unpause.
