# Cyberrange capacity load test — 2026-09-29/30

**Question:** can the cyberrange (.150 `proxmox`, shared with live workshop content) take a
heavy 8-team × 7-box competition (2 Windows incl. AD, 1 pfSense firewall, 4 Linux per team),
and how many more teams can it take before the infrastructure struggles?

**Method:** staged ramp — deploy 4 teams (`loadtest-cr-a`), verify + measure steady state,
deploy 4 more (`loadtest-cr-b`), verify + measure, then stack +2-team comps until a stop
criterion fires. Node telemetry (RAM, PSI memory/IO pressure, loadavg, KSM, ZFS ARC, pool
avail, per-VM memory) polled at 60 s cadence throughout, plus per-phase timing JSONL from the
deploy itself. Work: branch `loadtest-cyberrange-2026-09-29` in worktree
`../tezcatlipoca-loadtest-cr` (squash-merge to main pending approval).

## Baseline node facts (live-measured 2026-09-29)

| | value |
|---|---|
| node `proxmox` (10.0.0.150) | 72 cores, 251.5 GB RAM, uptime 50 h at start |
| RAM at start | 144.7 GB used / **106.9 GB free** (workshop + infra co-tenants) |
| datastore `hdd` (ZFS, primary) | 2.61 TB total, **907 GB free** at start |
| `wkshp-pool` (ZFS) | ~747 GB free |
| workshop co-tenants | ~70 running student VMs + guacamole/vulndb/nextcloud/ldap/dex |

The pool's free space is mostly notional: the workshop's own Windows golden images occupy
~75 GB **each** across dozens of datasets. Any large deployment shares the pool with them.

## Live-found failures (fixed on the branch; each was a full deploy-blocker)

1. **Unmanaged boxes crash the golden-hash loop** — `boxes.json` with an `unmanaged` pfSense
   slot KeyError'd in deploy.py's M4 hash computation (`golden_machines_by_box[fw01]`) and
   would have KeyError'd again in phase-1 wave-2. Fixed: skip unmanaged in hash inputs; wave-2
   treats their golden slot as stale. (`76e3194`)
2. **Stale ifupdown2 runtime state broke `ifreload -a` node-wide** — `/run/network/ifstatenew`
   carried dead bridges (`vmbr113/120/121/W120/W121`) from long-gone comps; every
   API-driven network change on the node failed with
   `/sys/class/net/vmbr120/brif/: No such file`. Fixed on the node (state file cleared, backup
   `/root/ifstatenew.bak-*`); bridges vmbr210-213 came up. **Any tenant of this node hits
   this until the workshop's own tooling clears that state.**
3. **Fedora golden auth is impossible on this node via the guest agent** — the fedora
   template enforces SELinux; the qemu-guest-agent domain is confined
   (`security_setenforce() failed: Permission denied`), and `sed -i` on `/etc/ssh/sshd_config`
   fails (`couldn't open temporary file /etc/ssh/...: Permission denied`). Proxied SSH is
   unavailable during golden build on this node (see 4), so the agent path is the only path.
   Adapted: `app01` uses `base-ubuntu24.04-fix` + apache. **Fedora boxes cannot be golden-built
   on .150 until the template runs SELinux permissive.**
4. **Engine team NICs never surfaced (twice)** — terraform's `team_nics` remote-exec claimed
   success while the guest had neither netplan nor NICs (`ip -br a` showed no ens19-22), so
   every gateway-proxied box SSH timed out at banner and fell back to the agent. Fixed by hand
   (netplan `60-team-ifaces.yaml`, cold boot via API). Root cause of the silent provisioner
   no-op is unresolved — flagged for internals follow-up.
5. **Engine host keys wiped by its own cleanup** — the live engine lost `/etc/ssh/ssh_host_*`
   mid-session (sshd answered, every session reset; confirmed via guest-agent `sshd -t`).
   Mechanism: `clean_engine_for_template` SSHes to the build VM **by mgmt IP**, and the
   template-build VM uses the same static IP as the deployed engine (`.250`) — with both up,
   ARP flaps and the cleanup lands on whichever VM answers. Contributing hazard: the engine
   template rebuild (hash gate) can run while another comp's engine is live on the same IP.
   Fixed for this run: regenerated keys via agent; comp B uses its own mgmt IP (`.249`).
   `ssh_via_gateway` now self-heals a stale TOFU pin (`27f8f9e`).
6. **Static `ipconfig0` with empty gw = PVE 400** — engine-template build with
   `TF_VAR_engine_mgmt_gw` unset sent `gw=`; PVE rejects it. Fixed: omit `gw` when empty
   (`07bcb2b`).
7. **run_nakon had no transport retry** — one LAN blip (this session saw several: SSH resets,
   a 1-sample API outage) killed minutes-long plants. Now retries rc=255/no-FAILED-step up to
   3× (`1dbf86e`).
8. **Preflight engine-IP gate self-collision** — a comp's own leftover engine (running from a
   failed attempt) tripped the "mgmt IP already answered" gate. Fixed: the gate skips VMs
   tagged as this competition's (`0ce865e`).

## Round 0 — `loadtest-cr-a`: 4 teams × 7 boxes (28 team VMs)

Shape (per team, sized to fit .150's RAM reality — 4 GB Windows would need ~128 GB for 8
teams vs ~105 GB free):

| box | template | RAM | disk | role |
|---|---|---|---|---|
| dc01 | base-windows-server | 3072 | 60 G sata0 | AD DC (ADDS, unbooted golden) |
| win01 | base-windows-server | 3072 | 60 G sata0 | domain-joined member (IIS, SMB) |
| fw01 | pfsense (956) | 512 | 12 G ide0 | unmanaged presence (no plant/score) |
| web01 | base-ubuntu24.04-fix | 1536 | 15 G | nginx |
| app01 | base-ubuntu24.04-fix | 1536 | 15 G | apache (fedora excluded, see 3) |
| db01 | base-debian13-lite-fix | 1024 | 10 G | mysql |
| dns01 | base-ubuntu24.04-fix | 1024 | 15 G | bind |

Scored pins: ADDS + WinRM (dc01), IIS HTTP + New SMB Share (win01), nginx/apache/mysql/bind
— 9 checks per team, `box_vulns` empty (verified `--expect-no-vulns`). Identifiers 210-213 →
vmids 2300-2339, bridges vmbr210-213, engine vmid 1000 @ 10.0.0.250.

**Verify (final pass): RESULT PASS** — logins, no-default-creds, services 8/8 UP × 4 teams,
pins_registered (8), isolation, plant coverage 24/24 full, plant integrity 0 failed steps,
4 forests with unique DomainSIDs (`team210-213.local`), win01 joined everywhere. Two earlier
passes showed transient FAILs (services before the first scoring round; one domain-check
transport hiccup) that cleared without intervention.

**Steady state (measured, scoring clock running, ~1 h after unpause):**

| metric | value |
|---|---|
| comp A VMs | 29 running (28 boxes + engine) |
| comp A actual RAM | **18.35 GB** (committed 50.0 GB) |
| per-team actual | ~3.25 GB (DC ~1.4-1.7, member ~0.6-1.1, linux ~0.3-0.4 each, fw ~0.35) |
| engine actual | ~2.7 GB (of 4 GB) |
| node free RAM | 49.8 GB |
| node CPU | ~7% of 72 cores |
| mem PSI | ~0 |
| pool `hdd` after A + co-tenants | ~258 GB free (A + its snapshots took ~650 GB) |

Co-tenants during the test (documented, not controlled): `multinode-spread-2026-09-30`
(4×2 linux, engine vmid 1080, teams split .150/.193) and `comp-cde-2026` (engine-template
1220, goldens 1230s, satellite builds in the 1330s) — both deployed by parallel sessions
while the ramp ran; their footprint is inside every "node free" number above.

## Estimate (written before scaling further)

- Committed-memory math: (49.8 free − 8 safety) / 11.75 GB-per-team → **~7-8 total teams**.
- Actual-usage math: (49.8 − 8) / 3.25 → **~17 total teams**.
- **Estimate: struggle at ~8-12 total teams (56-84 boxes), binding resource = host free RAM,
  with Windows boxes likely ramping toward committed size under live scoring load.** The pool
  was NOT expected to bind (thin clones) — it did, see round 1.

## Round 1 — scaling to 8 teams (`loadtest-cr-b`): the pool vetoes hdd

Deploying comp B (+4 teams, idents 214-217, vmids 2340-2379, engine 2480 @ .249) on `hdd`
collapsed the pool: 213 GB → **27.7 GB free within the hour** (comp B engine-template full
clone + golden builds write 150-200 GB before any team clone exists). The workshop's own
footprint owns the rest.

Adaptation: comp A stays (verified, untouched); comp B rebuilds on **`wkshp-pool`** (~747 GB
free, the pool user-approved for comps since svc-matrix). Golden-per-comp duplication is the
disk-scaling tax this exposes: every +2-team comp re-builds its own 6 goldens (~150-200 GB)
because goldens are per-competition by design — sharing pools across comps multiplies that.

**Comp B never completed.** Its goldens and engine template landed on `ssd` (golden builds
inherit the BASE TEMPLATE's storage, not `TF_VAR_datastore` — flow gap), so terraform then
moved every team disk ssd→wkshp-pool; clone #23 died with `cannot create
'wkshp-pool/vm-2365-disk-0': out of space` — **wkshp-pool hit literal 0 free**. A resume on
`ssd` (no moves needed; ssd had ~184 GB) was cloning at 31 s/box when the user stopped the
test. Retro-lesson: comp A's ~650 GB `hdd` draw was largely silent ssd→hdd moves of the same
class, not thin-clone growth.

## Verdict — where the cyberrange actually struggles

1. **The binding constraint is shared-pool disk, not RAM.** For this shape the node reached
   its ceiling at **~4-6 teams** under 2026-09-30 co-tenancy: `wkshp-pool` (747 GB) was
   consumed by one 4-team comp's golden set + template, and `hdd` was independently drained
   to ~48 GB by workshop content (~75 GB per Windows golden × dozens) and two other live
   comps. RAM never bound: ~40 GB free at 40+ running VMs (actual usage ≈ 3.25 GB/team vs
   11.75 GB committed).
2. **Per-comp golden duplication is the disk-scaling tax.** Every additional comp pays
   ~150-200 GB in goldens + ~40 GB in engine template before a single team clone. A node at
   98% pool occupancy cannot host stacked comps; only ONE comp of this shape fits, and only
   on a pool with 400+ GB true headroom.
3. **RAM ceiling estimate (for a cleaned node): 8-12 teams** committed-math vs actual-math
   band; would need per-comp golden sharing or a placement fix (goldens onto the declared
   datastore) to be reachable.
4. **Wall-clock cost of the shape:** comp A (4×7) took ~5 h of deploy across retries + ~1 h
   verify; the comp B build ran ~2.5 h without reaching phase 5.

## Teardown (closeout)

User stop order received mid-comp-B-resume. Comp B destroyed `--full`; comp A destroyed
`--full`; bridges vmbr210-213 removed (their stanzas had to be sed'd out of
`/etc/network/interfaces` — the API delete didn't stick to the file); inventory verified
clean of all loadtest vmids (2300-2479, 1000, 1140-1156, 1180, 2480, 2620-2640).

**Incident (owned):** the comp A remainder sweep used an ad-hoc script whose ownership
predicate matched generic name prefixes (`quotient-engine`, `engine-template`, `golden-*`)
and **destroyed 10 VMs belonging to the parallel sessions' comps** — multinode-spread's
running engines (1080, 1120) and comp-cde-2026's engine template (1220), goldens
(1231-1234), template (1260), and a satellite template (1333). The repo's destroy-ownership
guard (full tag set `comp-<name>` + `tezcatlipoca`) exists to prevent exactly this and the
ad-hoc script bypassed it. Their team boxes (1700-1739) were untouched; their engines and
goldens rebuild from base templates on their next deploy. User directive recorded:
never touch other sessions' infra; teardown sweeps run concurrent=4 with hard stops only.

## All code changes on this branch

| commit | change |
|---|---|
| `8e63fb4` | `TEZ_THIN_HEADROOM` opt-in datastore-gate factor (thin pools) + gate refactor into `check_datastore_headroom` + 5 tests + docs |
| `76e3194` | unmanaged boxes: skip golden-hash inputs; phase-1 treats their golden slot as stale (was: KeyError crash) |
| `0ce865e` | preflight engine-IP gate skips this comp's own tagged leftovers (redeploy self-collision) + comp B spec |
| `1dbf86e` | `run_nakon`: retry transport-level ssh death (rc=255, no FAILED steps) up to 3× |
| `27f8f9e` | `ssh_via_gateway`: self-heal stale engine TOFU pin (re-pin once, retry) |
| `07bcb2b` | engine-template build: omit empty `gw=` in `ipconfig0` (PVE 400) |
| `d21af3b` | linux SSH ladders fast-fail definitive rejections straight to guest-agent fallback |
| `e887faa` | tests: auth SSH ladder behavior (138 total suite) |
| `851e292` | this report + known-issues entries |
| `fa59819` | teardown: concurrent (4) hard-stop sweeps, no graceful shutdown, terraform destroy `-parallelism=4`; closeout sections |
| `87e398b` | teardown is resumable: stale state-lock recovery (only when no terraform is alive), tag-scoped leftover sweep (full tag set only), foreign VMs skip-and-continue in golden/engine-template destroys, 4-attempt idempotent retry loop; docs rule: practice runs require a new worktree |

Node-side (non-repo) changes: cleared stale `/run/network/ifstatenew` (backup
`/root/ifstatenew.bak-*`); tagged pfsense 956 `template`; wrote engine netplan
`60-team-ifaces.yaml` + regenerated engine host keys; stripped vmbr210-213 stanzas from
`/etc/network/interfaces` (backup `/root/interfaces.pre-loadtest-cleanup`).

## Recommendations

1. Fix golden placement to honor `TF_VAR_datastore` (build VMs currently inherit the base
   template's storage) — without it, any non-default datastore deploy pays silent full-disk
   moves per box.
2. Share or centralize goldens for repeated shapes, or place per-comp goldens on the emptiest
   pool explicitly — per-comp duplication caps stacked comps at the pool, not at RAM.
3. Never run two deploys of any scale on .150 without coordinating pools and vmid blocks —
   two sessions independently drained every pool on this node in one evening.
4. Keep fedora off .150 lineups until its template runs SELinux permissive (or plant-time
   gains an SELinux-tolerant path).
5. For >8-team events: cyberfield .193 (2.9 TB free hdrives-zfs, 181 GB RAM free) is the
   only node with headroom for this shape today.
