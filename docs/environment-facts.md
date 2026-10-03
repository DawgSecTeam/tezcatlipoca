# Environment facts — nodes, storage, templates, vmids

Hardware and environment reality for the Proxmox hosts this pipeline deploys onto. These are **not**
bugs — they are the ground truth a deploy has to fit into. Split out of
[known-issues.md](known-issues.md) so that
"something is broken" and "this is how the estate is" stop competing for attention.

**Verification column:** **[live]** = read back from the Proxmox API or a guest agent on 2026-10-02;
**[code]** = verified in the source tree; **[doc]** = inherited fact that cannot be re-verified
(historical, behavioral, or owner-reported). Anything marked **[live]** should be re-checked before
it is relied on — space and vmids move.

## Nodes

| Node | Endpoint | Env variant | Notes |
|---|---|---|---|
| cyberrange | `https://10.0.0.150:8006` | `.env.cyberrange-20260930` | node `proxmox`; shared with challenge/workshop/livefire VMs — the busy node |
| cyberfield | `https://10.0.0.193:8006` | `.env` | node `pve`; **known hardware instability** — can lose ping/SSH/API with no self-recovery and needs a physical power cycle. Running VMs come back intact, but the scoring loop needs `POST /api/competition/start` + `POST /api/engine/pause` (or `verify --fix-round-loop`). Owner-confirmed, not load-related. **[doc]** |

Only the Proxmox API (:8006) and the guest-agent channel are reachable from the dev host;
arbitrary VM ports TCP-RST. **Ping is not a liveness signal** for a node — a 10 h site-level outage
once left the tailnet bridge answering ICMP for itself while forwarding nothing. Probe the API port
(`curl -k https://<node>:8006/` — any HTTP code, not `000`). Deploy state (`.deploy_state.json`,
snapshots, VM disks) survives such an outage on the node; resume with `--from-phase`. **[doc]**

## Storage

Measured 2026-10-02 **[live]**:

| Datastore | Type | Node | Free / Total |
|---|---|---|---|
| `hdd` | zfspool | .150 | 197 GiB / 2613 GiB |
| `ssd` | zfspool | .150 | 383 GiB / 899 GiB |
| `wkshp-pool` | zfspool | .150 | 755 GiB / 899 GiB |
| `local` | dir | .150 | 145 GiB / 212 GiB |
| `local-lvm` | lvmthin | .150 | 16 GiB / 16 GiB |
| `hdrives-zfs` | zfspool | .193 | 2059 GiB / 4333 GiB |
| `local-lvm` | lvmthin | .193 | 117 GiB / 137 GiB |
| `local` | dir | .193 | 12 GiB / 66 GiB |

- **The headroom preflight under-counts by design, and Windows-heavy comps pay for it.**
  It compares free space against `teams × Σdisk_gb` of the *team* disks, but the real
  allocation is the **full clones**: one golden per box type plus the engine template.
  Live 2026-10-02, `same-type-2box-2026-09-29` on `hdd` (1 team × 90 GB provisioned):
  the preflight said "189 GB free vs ~90 GB needed" and the pool reached **0.2 GiB free**
  mid-run, at which point both `tz-base` and `tz-ready` snapshots failed with
  `zfs error: cannot create snapshot … out of space` — silently, leaving the range with
  **no rollback point**. Teardown returned it to 154.5 GiB, so the run itself consumed
  roughly 155 GiB: two goldens (30 + 60 GB) plus a ~40 GB engine template, i.e. the
  full clones dominate. `TEZ_THIN_HEADROOM` does not help here — these are full copies,
  not linked clones. Budget for goldens + engine template before trusting the gate, and
  tear down with `destroy-competition.py --full` as soon as a run's goal is met. **[live]**

- **The preflight headroom gate counts provisioned bytes** (≈ teams × Σ`disk_gb`), which does not
  match what linked clones actually allocate. `TEZ_THIN_HEADROOM=<0..1>` relaxes it by counting that
  fraction of the provisioned math. Size the factor to the pool (0.25 has been enough for
  Windows-heavy comps on `.150` `hdd`). **[code]**
- **Snapshot-capable datastores are ZFS, LVM-thin, Ceph, or qcow2-on-file.** Thick LVM cannot
  snapshot, so a range there has no `tz-base`/`tz-ready` restore points and `redeploy` falls back to
  `reconfigure`/`rebuild`. No thick-LVM datastore is currently in use on either node. **[live]**
- On .150, **only `wkshp-pool` comfortably fits a 10-box × 2-team comp** — `hdd` was down to 197 GiB
  at the last check (the 2026-09-29 note that it "recovered to ~900 GB" no longer holds). **[live]**
- Datastore saturation under parallel full clones produced pvestatd hangs and HTTP 596 errors; that
  is why terraform runs `-parallelism=1`. The Proxmox **task-wait budget is the bpg provider default
  of 1800 s, not a terraform setting** — and goldens override it via `constants.GOLDEN_CLONE_TIMEOUT`
  (5400 s), because the 1800 s default once aborted a healthy clone. The apply timeout is
  `600 + 300 × <target count>`, i.e. it scales with the number of VMs, **not** with disk size or
  Windows-vs-Linux lineage (an older claim that it scaled per Windows box was wrong). v2's team boxes
  are *linked* clones, so this pressure is mostly historical. **[code]**

## Templates

Cloud-init capability is what makes a template usable by this pipeline — a tagged template with no
cloud-init drive passes the preflight but its clones boot unreachable. Measured 2026-10-02
**[live]** (`ide2` etc. containing `cloudinit`):

**cyberrange .150 — usable:** `955 base-ubuntu24.04-fix`, `951 base-debian13-lite-fix`,
`1016 base-fedora44-fix`, `127 base-alpine3.23`, plus the challenge/workshop templates tagged
`cloud-init` (`112`, `132`, `140`, `153`, …).

**cyberrange .150 — NOT usable (no cloud-init drive):** `106 base-ubuntu24.04`,
`920 base-debian13-cloudinit` ⚠️, `103 base-ubuntu20.04`, `109 base-fedora44`.

> ⚠️ **vmid 920 is named `base-debian13-cloudinit` but has no cloud-init drive** (its .193 sibling,
> `1005`, is the same). Do not trust the name — check the config. The old known-issues entry called
> it "920 / debian13-lite"; the vmid was right and the name was stale.

**cyberfield .193 — usable:** `1007 base-ubuntu24.04-fix`, `1006 base-debian13-lite-fix`,
`1015 base-fedora44-fix`, `1019 base-alpine3.23-fix`, `1032 base-centos8-fix`,
`1033 base-ubuntu20.04-fix`, `1220 engine-template` (cde-2026).

**cyberfield .193 — NOT usable:** `1001 base-ubuntu20.04`, `1002 base-ubuntu24.04`,
`1005 base-debian13-cloudinit` ⚠️, `1004 base-alpine3.23`, `1003 base-fedora44`, and the
`challenge-*` / `workshop-*` templates that carry no `cloud-init` tag.

**This is enforced, not just documented** (since 2026-10-02): the single-node and multinode
preflights read each selected Linux template's config and refuse one with no cloud-init drive.
**[code]**

**Distro-specific template requirements:**

- **`base-fedora44` ships without cloud-init** (cannot take identity). Build a `-fix` variant
  (`.150` → `1016`, `.193` → `1015`); Fedora also needs SSH password-auth forced via a `00-*` drop-in
  (sshd is first-match-wins), `named` bound to more than `127.0.0.1`, and `httpd` with
  `ServerName localhost`. Per-box httpd/named fixes are folded into `fix_services_on_boxes`.
  **[code/doc]**
- **Alpine templates need persistent `ssh_pwauth` and a `%wheel` rule in the MAIN sudoers file.**
  The image ships `PasswordAuthentication no` + `ssh_pwauth: false`, and a clone's first-boot
  cloud-init re-disables password auth even if the template fixed `sshd_config`; Alpine's sudo also
  ships no `%wheel` rule, so a NOPASSWD entry living only in `sudoers.d` vanishes the moment a
  `writable-sudoers`-class plant makes that dir 0777. Anything that sh-commands `sudo` on a
  post-sweep box must assume password-sudo at best. **[doc]**
- **Fedora + the guest agent = confined.** Live-confirmed 2026-10-02 on a Fedora 44 clone on .150:
  guest-exec runs as root but inside `virt_qemu_ga_t` (SELinux enforcing), where `dnf`/`rpm` are
  `Permission denied`, `/etc` is unwritable, `systemctl` is `Access denied`, and both `setenforce 0`
  and the `virt_qemu_ga_run_unconfined` boolean are denied. **SSH is the working path** — logging in
  as the image's cloud user with the repo key lands in `unconfined_t` with passwordless sudo, i.e.
  real root. Since the pipeline plants over SSH, this only blocks agent-only flows (the golden-build
  fallback): keep Fedora out of lineups where the agent is the only path, or flip
  `/etc/selinux/config` to permissive at template-build time. **[live]**
- **Windows:** the stock template ships all firewall profiles disabled, so firewall-rule effects do
  nothing until `Set-NetFirewallProfile -All -Enabled True` (now done by `bootstrap_windows_box`,
  along with enabling `RemoteDesktop-UserMode-In-TCP/UDP` — the DC template ships that rule group
  disabled, so `fDenyTSConnections=0` alone does not make RDP reachable). A Windows clone's guest
  agent takes ~8 min to appear (bootstrap polls within a 900 s deadline). **[live/code]**

## vmid occupancy

**Default team identifiers (101+) collide on both nodes.** Observed 2026-10-02 **[live]**:
on .150 vmids 100–124 are all occupied (challenge, workshop, livefire, base and service VMs); on
.193 vmids 100–118 are largely occupied. The vmid-collision preflight names the conflict; pass
`TF_VAR_team_identifiers` explicitly for a predictable block.  **[doc + live]**

**Engine management IPs are shared unless you separate them.** `clean_engine_for_template` delivers
its cleanup by SSH to the *planned* engine mgmt IP, so two engines (or an engine and a build VM) on
one node can ARP-flap and the cleanup can land on the wrong, live engine — observed once destroying
another engine's `.env`, `event.conf` and host keys. Give each concurrent comp its own
`TF_VAR_engine_mgmt_ip`. See the open item in [known-issues.md](known-issues.md). **[code + doc]**

**Un-reclaimed ranges are a standing estate problem.** At the last check, 43 tezcatlipoca VMs from
three past competitions were still on the nodes, 20 of them running:

| Node | Competition | VMs | Running | Surviving state |
|---|---|---|---|---|
| .150 | `scale8-scrim-2026-10-01` | 16 | 10 | branch `scale8-2026-10-01`, `.tez-backups/scale8-preserve/` |
| .193 | `cde-2026` | 14 | 9 | `competitions/cde-2026/` |
| .193 | `scale8-scrim-2026-10-01` | 7 | 1 | same branch/backup |
| .193 | `amongus-cde-2026` | 6 | 0 | `competitions/amongus-cde/` |

They hold vmids, consume `hdd`, and their golden templates can squat a slot a concurrent comp
wants. Reclaim with `destroy-competition.py` from the matching comp dir/worktree — never an ad-hoc
sweep. **[live]**

## Node runtime behavior

- **.150 (cyberrange) RAM is the binding constraint for satellite-heavy Windows ranges.** The
  2026-10-02 soak measured 50–70 GB *available* against our ~48 GB committed for 4 satellite teams
  (dc01+win01, plus the jump and slot-1 goldens) — a ~2 GB margin on a node also carrying 73
  co-tenant VMs; KSM and ARC absorbed it, but nothing else would have. Before placing 4
  Windows-heavy teams there again, choose deliberately: trim `dc01`/`win01` memory (check the
  Windows pagefile/commit charge first), reduce co-tenancy, or split 6/2 instead of 4/4 and treat
  .150 as the 4-team ceiling. Deploy time budget:
  [multi-node.md § Phase budget](multi-node.md#phase-budget-8-teams--5-boxes-two-nodes). **[live]**
- **The management network is ONE flat `10.0.0.0/24` L2 shared by both hosts.** A VM on .193 and a
  VM on .150 are on the same segment, so `TF_VAR_engine_mgmt_ip` (and `jump_mgmt_ip`) are contested
  **cluster-wide**, not per node — the engine's default `10.0.0.250` is what every competition gets
  unless someone sets otherwise. Live-found 2026-10-02: `10.0.0.250` was a foreign competition's
  live `quotient-engine` (verified: `ssh sysadmin@10.0.0.250 hostname` → `quotient-engine`) while a
  .150 practice deploy was handed the same address by default; the build VM came up on it and died
  at phase 2 with an opaque `ssh … exit status 255`. **Always set an explicit, checked
  `TF_VAR_engine_mgmt_ip` when anything else is running on the estate**; `.245`, `.246`, `.249` and
  `.250` all answered on 2026-10-02. The preflight now scans cluster-wide and refuses the *default*
  address when any guest is unverifiable (see `config_ops._engine_mgmt_ip_gate`). **[live/code]**
- **netplan refuses world-readable configs** — the engine's team-NIC netplan files are mode `0600`.
  Verified on both running engines 2026-10-02. **[live]**
- **Docker re-syncs iptables on every container start/restart**: `FORWARD` goes to `DROP` and the
  custom team-subnet MASQUERADE + team-to-team DROP rules vanish. Mitigations are live and verified
  on both running engines: `range-firewall.timer` and `range-healthcheck.timer` **active**,
  `FORWARD` policy `ACCEPT`, 3 MASQUERADE rules present, 9 Quotient containers up.
  `ensure_nat_forwarding` also re-asserts the rules before each nakon pass. **[live/code]**
- **Quotient crash-loops without `event.conf`**, so it is pushed before nakon ever runs. **[code]**
- **New VirtIO NICs need a cold boot** — a guest-level reboot does not trigger the PCI scan, so the
  engine's team NICs are addressed only after a hypervisor-level stop/start. **[doc]**
- **Fresh clones can boot with an empty `/etc/resolv.conf`** (cloud-init ignores `dns.servers` when
  the IP is static) and nakon installs services with apt — so `fix_dns_on_boxes` exists and runs in
  the deploy. **[code]** Related: never point boxes at their own team's `dns*` box from Terraform —
  deadlock (bind is installed by nakon via apt, which needs a working resolver). **[doc]**
- **Stale ifupdown2 runtime state breaks `ifreload -a` node-wide**: `/run/network/ifstatenew` can
  carry dead bridges from long-gone comps, and every API-driven network change then fails on a
  missing `/sys/class/net/vmbrNNN/brif/`. **The API list does not show this state.** Repair on the
  node: `rm /run/network/ifstatenew && ifreload -a`. Tear team bridges down with `ifdown`, never raw
  `ip link del` (which is what creates the stale state). **[doc]**
- **Quotient secrets are plaintext-but-`0600` on the engine** (`/opt/quotient/.env`), generated fresh
  per deploy. Box/credlist usernames are themeable and not secret; the passwords always are.
  **[live/code]**
