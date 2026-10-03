# Architecture

> **Pipeline version 2** — golden templates + linked clones, phases renumbered (2026-09-24).
> Everything in this document describes v2 unless a passage is explicitly marked "v1".
> `deploy.py` refuses to resume a `.deploy_state.json` written by another pipeline version
> (`pipeline_version`), so v1 and v2 phase numbers must never be mixed. See
> [v1 → v2 migration](#v1-to-v2-migration-what-moved) for what changed.

## Overview

`tezcatlipoca` is a Proxmox driver that builds a per-competition scoring range.
`create-competition.py` (via `deploy.py`) runs an eight-phase deploy: it generates Quotient scoring
config and a nakon machine list, provisions Proxmox resources with Terraform, bootstraps the
scoring engine, and drives nakon to plant services and misconfigurations. Quotient scores; nakon
provisions. The scoring engine is the only host with a NIC on every team bridge — it is the SSH
jump host, the nakon execution host, and the NAT gateway. Team isolation is enforced by iptables on
the engine, not by bridge separation.

Per-symbol design notes and the "why" behind specific parameters and orderings live in
[internals.md](internals.md); open problems live in [known-issues.md](known-issues.md);
the agent-scrim harness in
[scrim-harness.md](scrim-harness.md).

## Component map

| Path | Purpose |
|---|---|
| `create-competition.py` | Thin CLI entry point: loads `.env`, calls `deploy.main()`. Not importable (the hyphen), so it exposes no library surface |
| `pipeline_api.py` | The stable import surface the hyphenated entry points cannot be: explicit `__all__` of the symbols `redeploy-competition.py` calls, each imported from the module that owns it |
| `deploy.py` | Deploy orchestration + CLI: `prepare()` (config/state/secrets/placement/hashes/tfvars/preflight) and the sequencer `deploy()`, which walks `PHASES` |
| `deploy_phases.py` | The eight phase functions (`phase1_cleanup` … `phase8_seed`) plus `connect_terraform`/`finish_deploy`/`destroy_node_waves` — all the per-phase work |
| `config_ops.py` | Competition prompts and persistence: `Compfile`, `boxes.json`, `teams`, `users.json`, `injects`, `.env` updates |
| `nakon_ops.py` | `generate_nakon_config` / `generate_stage_configs` / `build_nakon_bundle` / `run_nakon`; `os_to_platform` (mirrors nakon) |
| `golden_ops.py` | M3 golden set: API-clone one VM per box type, plant the golden stage (strict), `cloud-init clean`, **boot-smoke** the stopped golden via one throwaway clone (`golden_boot_smoke` knob), `qm template` conversion; resume routing on template state |
| `template_ops.py` | M4 per-competition template lifecycle: content hashes, engine-template build/clean/convert, reuse-vs-rebuild gates, freeze state |
| `template_sync_ops.py` | Cross-node template sync (`vzdump --stdout \| ssh qmrestore`) so satellites hold their own box templates |
| `timing.py` | M0.1 deploy timing: per-op JSONL sidecar `.deploy-timings.jsonl` + end-of-run summary |
| `windows_ops.py` | Windows bootstrap over QEMU guest agent (static IP, password, sshd) + DC/member waits |
| `hardening_ops.py` | DNS repair (`fix_dns_on_boxes`), service hardening, Ubuntu password-auth setup, boot-settle probe, apt-cacher proxy |
| `domain_ops.py` | Per-team AD forests: promote DC (`ADDS`), join members (`Domain Join` / `domain-join` via realmd); teams run concurrently |
| `engine_ops.py` | Engine bootstrap (Docker, Quotient), `push_event_conf`, NAT/firewall timers, `ensure_nat_forwarding` |
| `nodes_ops.py` | Multi-node placement: `nodes.json` config, capacity-fill team placement, the per-competition `placement.json`. See [multi-node.md](multi-node.md) |
| `jump_ops.py` | Per-satellite jump/router VM: impersonates the engine's gateway IP on the satellite's team bridges, DNAT/SNAT, default-DROP forwarding |
| `routing_ops.py` | Post-apply #1 fail-loud gate proving the engine can reach every satellite (the routes themselves are written by `team_nics`) |
| `firewall_ops.py` | In-path firewalls: per-team pfSense `config.xml` generation from the comp's seed, QEMU-monitor console bootstrap (`sendkey` + functional SSH probes), the engine netplan cutover, and the post-cutover routing gate |
| `ssh_ops.py` | Gateway SSH (`ProxyCommand -W`), Terraform context, `wait_for_ssh`/`wait_for_boxes_ssh`/`wait_for_http` |
| `constants.py` | `vmid` math, `MAX_TEAMS`/`MAX_BOXES_PER_TEAM`, `SNAP_BASE`/`SNAP_READY`, budgets, `NAKON_DIR`, Windows user |
| `range_ops.py` | Proxmox API, guest-agent exec, VM lifecycle, `enumerate_targets`, snapshot helpers, `vm_id_for` |
| `utils.py` | `load_compfile`, `load_users_config`, `DNS_FIX_CMD`, `run_concurrent`, competition picking |
| `quotient/setup.py` | `build_event_conf`, `seed_teams`, `unpause_engine`, `create_injects`; `_SERVICE_TO_CHECK` map |
| `terraform/main.tf` | Team bridges (`vmbr<id>`), transit bridges (`vmbrW<id>`, firewall lineups only), scoring VM (`var.scoring_vm_id`), every team's boxes as slot-dimensioned resources (two NICs + no cloud-init for the in-path firewall), scoring NIC wiring, cold-boot + netplan |
| `vendor/nakon` | Submodule (pinned release) — CLI-only catalog access and bundle builder |
| `verify-competition.py` | Post-deploy checks (login, services, isolation, misconfig, injects, pins, plant coverage, domains, red identity, packet) on a tri-state gate model — PASS / FAIL / SKIP, where a SKIP is non-passing unless waived with `--allow-unverified` |
| `redeploy-competition.py` | Filtered per-team/box rollback/reconfigure/rebuild/resync/engine-recovery using snapshots |
| `destroy-competition.py` | Tears down all team boxes + `terraform destroy` (+ golden templates on `--full`) |
| `packet_ops.py` | Packet-profile load/validate/compile into a competition bundle + fidelity report (inverse of `generate-packet.py`) |
| `compile-packet.py` | CLI over `packet_ops`: `packets/<event>/packet.yaml` → `competitions/<id>/` |
| `run-schedule.py` | Event-day schedule driver over a packet's `schedule:` windows (start / freeze / resume / end) |
| `sync-template.py` | Thin CLI over `template_sync_ops` |
| `generate-packet.py` | Renders `competitions/<id>/packet.md` from `Compfile`/`boxes.json`/`box_services.json` |
| `beacon_ops.py` | Plants unscored, non-destructive C2-style beacons on team boxes (before `tz-ready`) for blue to hunt |
| `run-agent-scrim.py` | Red-vs-blue agent scrim: cycles LLM agent sessions against the live range, snapshots the scoreboard |
| `scrim-report.py` | Scores a scrim run from red's `events.jsonl` + scoreboard snapshots (interaction score, gates) |
| `terraform/variables.tf` | `TF_VAR_*` inputs; `outputs.tf` exposes `agent_context` for the driver |

`beacon_ops`, `run-agent-scrim`, and `scrim-report` are documented in
[scrim-harness.md](scrim-harness.md), not in [internals.md](internals.md).

## Eight-phase deploy

Pipeline v3 (2026-10-03) added the in-path firewall bootstrap (phase 5) and renumbered
phases 5-7 to 6-8. Pipeline v2 (golden templates + linked clones, 2026-09-24) preceded it.
`--from-phase` requires a state file written by the same pipeline version; cross-version resumes are refused
(`pipeline_version` in `.deploy_state.json`). `--from-phase` requires a
state file written by the same pipeline version; cross-version resumes are refused
(`pipeline_version` in `.deploy_state.json`).

1. **Cleanup** — two parallel destroy waves (worker pool of 8, ownership-tag checked):
   wave 1 destroys every team box (the computed vmid set covers all teams — Terraform
   builds them all now) plus any legacy `cloned_vms.json` entries; wave 2 destroys the
   scoring engine plus every golden template that is missing, hash-mismatched, or a
   stale slot beyond the lineup. M4: **hash-matching golden templates survive cleanup** —
   that is the test-run reuse (build once per competition, reuse across ITS runs — never
   across competitions: the hash inputs are per-competition, so a different competition
   always rebuilds them; when a run has achieved its goal, `destroy-competition.py --full`
   tears the golden range down). The
   engine template is never touched here. Linked clones must die before their
   templates. Bridges serial. Skipped on `--from-phase >1`.
2. **Engine template + Terraform apply #1** — M4 first computes the engine template
   hash (base image vmid + pinned `quotient_ref` + bootstrap/main.tf code) and reuses
   the competition's engine template when it matches; on drift (and never when frozen —
   config drift hard-fails, code drift warns) it destroys and rebuilds it: API
   full-clone of the base image, bootstrap, then the clean step (containers + volumes
   removed, `.env`/host keys removed, machine-id truncated, cloud-init cleaned;
   apt-cacher-ng installed but its cache is empty — the useful cache lives on the deployed
   engine clone and is lost on teardown) and `qm template`. `terraform apply -parallelism=1` with
   `build_team_boxes=false` then creates the engine as a **linked clone of that
   template** (`engine_clone_id`), every team bridge, the engine's team-facing NICs
   (netplan) and its cold boot. Waits for engine SSH. Multi-node: the per-satellite
   jump/router VMs are then built on a bounded pool of 4 (independent hosts, direct API),
   then `routing_ops` fail-loud gates engine→jump reachability.
3. **Prepare engine from template** — per-deploy state applied fresh: `.env` written
   BEFORE `compose up` (so the fresh postgres volume initializes with this
   competition's credentials), `compose up` on volumes the template no longer contains
   (an **empty scoring DB every run**), apt-cacher-ng check, `event.conf`/
   `linux.credlist` push early, NAT re-assert. (Until M4, phase 3 was the full
   bootstrap — packages, Docker, Quotient build, ~459 s; that work now happens once on
   the template build VM and every deploy re-clones it.)
4. **Golden set + apply #2** — `golden_ops` full-clones one VM per box type from the base
   templates onto team1's subnet at `.240+`, plants the **golden stage** (everything
   outside `constants.POST_CLONE_CONFIGS`) in strict mode under a tz-base rollback guard, cleans
   cloud-init state, and **boot-smokes** the result: with the goldens stopped, one throwaway
   FULL clone of each booted golden is booted and must reach multi-user (guest agent for
   Windows) before anything converts — a failed or unverifiable smoke raises and blocks
   `POST /template` (Compfile `golden_boot_smoke 0` skips the gate and warns that the
   invariant is UNVERIFIED; cold/unbooted DC goldens deliberately skip it). Only then are
   the VMs converted with `qm template` (the per-box
   hash lands on the template description + `.template-hashes.json`; matching templates
   were already skipped above). Then
   `terraform apply #2` (`build_team_boxes=true`) creates **every team's boxes as linked
   clones** of the golden templates — `cloned_vms.json` and the team1 special case are
   gone. Windows bootstrap across all teams (guest agent), SSH/cloud-init waits with
   per-box budgets, `setup_ubuntu_auth`, DNS fix, apt prep, `tz-base` snapshots. The
   snapshot / password / cloud-init-clean / convert passes over the golden set run on a
   bounded pool of 4.
5. **Firewall bootstrap (in-path firewalls only)** — skipped entirely unless the
   lineup declares an `unmanaged` + `in_path` box. Terraform apply #2 cloned each
   team's firewall from its own template with two NICs (WAN on the per-team transit
   bridge `vmbrW<id>`, LAN on `vmbr<id>`); this phase generates one pfSense
   `config-team<id>.xml` per team (`firewall_ops`, from the comp's
   `pfsense/pfsense-config-orig.xml` seed), serves it from the engine, drives each
   firewall's console to fetch it (`sendkey` via the QEMU monitor API), waits for the
   config's SSH to answer on the WAN address, then **cuts the engine over**: the
   engine's netplan loses `192.168.<id>.1` (the firewall owns the team gateway now)
   and gains `192.168.<id>.0/24 via 172.31.<id>.2` routes. A fail-loud gate proves
   every team subnet routes through its firewall and the first managed box is
   reachable through it. Caveat: a later `terraform apply` rewrites the pre-cutover
   netplan (see [internals.md](internals.md#firewall_opspy)).
6. **Repair-stage sweep** — the first post-clone pass plants the **repair stage**
   (sshd/sudoers touchers) on every team box, lenient, with `--jobs`; then
   `fix_services_on_boxes` on **Linux boxes only** (Windows never reaches the bash
   executor; sshd un-wedge, credlist accounts, service binds). Failures
   are recorded per machine (stage-prefixed tally + plant-coverage record) in
   `.deploy_state.json`. Marked by `.postclone-swept` for idempotent resume.
7. **Domains → final pass → beacons + tz-ready** — per-team AD forests run
   **concurrently** (each team's ADDS/join chain is serial within itself); then the
   **final-stage pass** plants the disruptive (DNS/apt-breaking) + boot-hostile configs
   AFTER the domain joins — those reboots are exactly what boot-hostile configs would
   brick, and the realmd joins need working DNS/apt (see `constants.py` for the split).
   Beacons, then the `tz-ready` snapshot; snapshot pool of 4. (The firewall, like
   every team box, is snapshotted at `tz-ready`; its `tz-base` lands at the END of
   phase 5, after the cutover — a pre-cutover restore point would be a firewallless
   network pretending to be in-path.)
8. **Seed** — unchanged: poll Quotient HTTP, `seed_teams`, `unpause_engine`,
   `create_injects`; each gated on `.deploy_state.json` flags.

## v1 to v2 migration (what moved)

The pre-golden ("v1") pipeline is no longer in the tree; this table exists so a passage
written against it can be read correctly. `--from-phase N` numbers mean different things in
the two generations, which is why `deploy.py` hard-refuses a cross-version resume
(`pipeline_version` in `.deploy_state.json`).

| Phase | v1 (historical) | v2 (current) |
|---|---|---|
| 1 | destroy-prior-range | Cleanup (two waves; hash-matching goldens survive) |
| 2 | `terraform apply` (team1 boxes + engine + bridges) | engine-template build/reuse + apply #1 (`build_team_boxes=false`) |
| 3 | no-op (the SSH-key copy it used to do was removed) | prepare engine from template (fresh volumes, `event.conf`) |
| 4 | bootstrap engine from scratch (~459 s) | golden set + apply #2 (every team as a linked clone) + `tz-base` |
| 5 | nakon on team1 (**strict**) | repair-stage sweep (**lenient**) + `fix_services_on_boxes` |
| 6 | clone team1 → team2+ over the Proxmox API, then nakon there | domains → final-stage pass (lenient) → beacons + `tz-ready` |
| 7 | seed/start | seed/start (unchanged) |

What actually changed:

- **One full clone per box type, not per team.** The golden set (phase 4) full-clones each
  box type once, plants it, and converts it with `qm template`; `terraform apply #2` then
  creates every team's boxes as **linked clones** of those templates. Storage cost collapses
  and the golden-stage installs ride the clone instead of being re-planted per team.
- **Cloning moved into Terraform.** v1's `clone_team_boxes` API clones and
  `cloned_vms.json` bookkeeping are gone; all teams are Terraform-managed via
  `var.golden_template_ids` (+ per-slot maps). `cloned_vms.json` survives only as a
  legacy destroy path.
- **The engine is templated.** v1 bootstrapped Quotient on the deployed engine in phase 4.
  v2 builds a per-competition engine template in phase 2 and linked-clones it; phase 3
  applies only per-deploy state, so the scoring DB starts empty every run.
- **`strict` narrowed to one pass.** Only the phase-4 golden plant is strict; phases 5 and 6
  pass `strict=False` (a flaky plant must not kill a sweep after 98% landed).
- **The post-clone configs split in two.** A single post-clone set became `repair`
  (phase 5) and `final` (phase 6, after the domain reboots) — four stage files total
  (`.nakon-golden/-repair/-final/-postclone.json`).
- **Resume markers renamed.** `.phase6-swept` → `.postclone-swept` (written by phase 5).
- **`tz-base` moved.** It is now taken once for **all** boxes at the end of the phase-4
  block, not per-generation of clone.

## Data flow

```
Compfile + boxes.json  (per-competition, tracked or pinned)
        |
        v
box_services.json / box_vulns.json  (randomized via nakon or pinned; per box type)
        |
        v
nakon-config.json  (per-machine expansion: team x box type -> ip/user/password/configurations)
        |
        generate_stage_configs splits it into THREE passes / four files (M3.2):
        |
        +--> .nakon-golden.json     one machine per BOX TYPE at the golden IP,
        |                           golden-stage configs (everything but the
        |                           repair ∪ final subset) --> pass 1, STRICT, phase 4
        |
        +--> .nakon-repair.json     every team machine, repair-stage subset
        |                           (sshd/sudoers touchers) --> pass 2, lenient, phase 5
        |
        +--> .nakon-final.json      every team machine, final-stage subset
        |                           (disruptive + boot-hostile, last) --> pass 3,
        |                           lenient, phase 6
        |
        +--> .nakon-postclone.json  repair ∪ final — the convergence view
        |                           redeploy's rollback/reconfigure modes replay
        |
        v
three nakon bundles  (content-addressed under vendor/nakon/bundles/, cached by config hash)
        |
        v
engine /opt/nakon/<run-tag>/  (per-run staging; `nakon deploy --bundle ... --config ...
                               [--only ...] [--strict] [--jobs N]` via SSH)
        |
        v
Quotient /opt/quotient  (event.conf + linux.credlist + .env from phase 3)
```

- `Compfile` (`name`/`scenario`/`difficulty`) and `boxes.json` (`name`/`template`/`cpu`/`memory_mb`/`disk_gb`/`last_octet`) are the only required inputs; `users.json` and `domain_roles.json` are optional.
- `box_services.json`/`box_vulns.json` are keyed by box type, not per-team; every team defends the identical set so Quotient wildcard checks (`192.168._.<octet>`) remain valid.
- `nakon-config.json` expands the type-level selection to per-machine entries (`id`/`name`/`ip`/`os`/`user`/`password`/`configurations`); disruption configs are sorted last.
- **Stage-split rationale (canonical).** Linked clones inherit the golden disk, so everything
  identity-free and non-disruptive plants **once** per box type (golden stage) and costs
  nothing per team. What cannot ride the golden disk is small and splits by *ordering
  constraint*: `repair` (sshd/sudoers touchers) must land post-clone **before** the domain
  pass, because nakon's member joins authenticate over SSH and a clone's fresh cloud-init can
  drift that state; `final` (disruptive DNS/apt breakers + boot-hostile configs) must land
  **after** the domain joins, because those joins reboot member boxes and need working
  DNS/apt. `constants.REPAIR_STAGE_CONFIGS` / `FINAL_STAGE_CONFIGS` carry the per-config
  membership and its history; `POST_CLONE_CONFIGS` is their union.
- Bundles are built operator-side; the engine never sees vulndb creds. `--only` scopes deploy without changing bundle content.

## Target abstraction

`range_ops.enumerate_targets(teams, boxes)` builds one entry per `(team, box)`:

- `vmid` = `200 + identifier*10 + box_index` (stride 10, mirrored in `terraform/main.tf`; `vm_id_for` is the sole derivation).
- `ip` = `192.168.<identifier>.<last_octet>`; `vm_name` is `team1-<box>` vs `<identifier>-<box>` for clones; `machine` is `<box>-team<identifier>` (nakon naming).
- Limits: `MAX_BOXES_PER_TEAM=10`, `MAX_TEAMS=154` (identifiers `101`-`254` map to `192.168.101.0/24` … `192.168.254.0/24`).

Invariant: build from the full team/box lists first, then filter. Never filter `boxes` and
re-enumerate, because `vmid` is positional — a filtered re-enumeration would assign wrong IDs and
collide with existing VMs. `deploy.py` and `redeploy-competition.py` both follow this.

## Windows path

Linux boxes use cloud-init (`initialization` block in `terraform/main.tf`): creates `box_username`,
pushes `ssh_public_key`, sets `box_password`, sets DNS to `8.8.8.8`, assigns `ip/gateway/bridge`.
Windows boxes skip that block entirely (template name contains `win`, case-insensitive).

- Post-clone, `bootstrap_windows_box` (via `guest_agent_exec_windows`, base64-encoded PowerShell) configures the single `Up` adapter with static IP/gateway/DNS, sets the `Administrator` password (nakon uses it via paramiko), and enables `sshd` + `QEMU-GA` with a firewall rule for port 22. This is the only channel before networking or credentials exist.
- Templates are sysprepped with `sysprep /generalize /oobe /shutdown /unattend:<xml>` where `<ComputerName>*</ComputerName>` yields a fresh random hostname/SID per clone.
- The `win` substring is the single classifier: `os_to_platform` in `nakon_ops.py` (mirrors nakon) routes catalog configs, and `terraform/main.tf` skips `initialization` on the same predicate.

Domain-joined boxes are not part of the main bundle. `domain_ops.deploy_domain_configs` reads
`domain_roles.json` (`{"dc01":"dc","member01":"member"}`) and, per team, promotes the first `dc`
box to a new forest `team<identifier>.local` (`ADDS` with `dsrm_password = box_password`). DC
box types use an unbooted golden, so each linked clone specializes its own machine SID before
promotion and therefore receives a unique DomainSID. After the promotion reboot, the driver
waits for guest agent + `sshd`, then AD Web Services (`Get-ADDomain`) before planting AD-aware
misconfigs. It waits for DNS SRV (`_ldap._tcp.dc._msdcs.<domain>`) before joining each `member`:
Windows members via `Add-Computer` after repointing DNS to the DC, Linux members via nakon's
`domain-join` (`DOMAIN`/`DC_IP`/`DOMAIN_ADMIN_*`/`BOX_HOSTNAME`, realmd/sssd). Membership is
probed after joins and transient failures retry up to three times. Each rebooting step is an
isolated single-machine nakon pass because `ADDS`/`Domain Join` reboot and would truncate a
combined plan. Skipped entirely if `domain_roles.json` is absent — and fail-closed when it is
present but invalid: an unparseable file, a role value outside `dc`/`member`, or a role box
absent from `boxes.json` is an error at both readers that decide golden layout or verification
(`golden_ops.unbooted_golden_boxes`, `verify-competition.check_domains`), never a silently
skipped entry.

## Network & isolation

- Team bridges (`vmbr<identifier>`, `proxmox_network_linux_bridge.team_bridge`) are created with no `ports` (no physical uplink). Isolation does not come from bridges — the engine has a NIC on every bridge and would route between them if not firewalled.
- The scoring engine has one `virtio` NIC per team bridge (dynamic `network_device` for each `var.teams` entry) plus `vmbr0` for management. Team NIC addresses are applied via netplan (`/etc/netplan/60-team-ifaces.yaml`, `ens19` onward, `192.168.<id>.1/24`) after a cold-boot (`null_resource.reboot_scoring_engine` stop/start) so new VirtIO NICs are PCI-enumerated. `ip_forward` and `rp_filter` are set via sysctl.

The engine forwards and NATs:

- `FORWARD` chain: `-s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP` (team-to-team isolation, inserted at line 1).
- `POSTROUTING`: `-s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE` (team Internet access for `apt-get`).

Docker resets `FORWARD` policy to `DROP` and wipes custom rules on every start/restart.
`engine_ops.bootstrap_scoring_engine` installs:

- `range-firewall.sh` + `range-firewall.service` (`After=docker.service`) + `range-firewall.timer` (OnBootSec 30s, OnUnitActiveSec 30s) to re-assert both rules for the life of the range.
- `range-healthcheck.sh` + `range-healthcheck.service` + `range-healthcheck.timer` (60s) logging failures for Quotient containers/API and both iptables rules to `/var/log/range-healthcheck.log`.

`ensure_nat_forwarding` re-asserts the same rules idempotently before each nakon run, covering the
gap before the timer is installed.

### Red team identity (bad-auto side, not this pipeline)

Red's network mode is owned by `../bad-auto` (`badauto/deploy/engine_nat.py`, `deploy.red_mode`
in its config), installed onto the engine at red-deploy time as its own `bad-auto-firewall`
unit + 30s timer alongside `range-firewall`:

- **routed** (default): red01 carries a secondary address on a dedicated segment (default
  `10.200.0.0/24`; the engine's mgmt iface holds the gateway `.1` via an idempotent `ip addr
  add` in the re-assert script — never a netplan override, which would replace the mgmt
  iface's address list). Attacks keep the red-segment source end-to-end, so blue can hunt and
  firewall the attacker; scoring still sources from the team gateway.
- **masq** (legacy): red01 is MASQUERADEd into the team gateways — red unblockable-by-IP
  (indistinguishable from scoring).

Both modes: `FORWARD ACCEPT red→192.168.0.0/16`, an ESTABLISHED accept for return traffic
(ordered before the DROP), and `FORWARD DROP toward red` so blue cannot counterattack red
infrastructure. The raw-socket beacon C2 DNAT (boxes → their gateway `:port` → red01) rides
the same engine. `verify-competition.py --red-identity` proves the routed source address
survives to the boxes.

## Snapshots

| Snapshot | When | Content | Scope |
|---|---|---|---|
| `tz-base` | Golden boxes: after boot/auth/DNS/apt prep, **before the golden plant** (phase 4). Team boxes: after boot/auth/DNS/apt prep, before the post-clone sweep (phase 4) | Booted, networked, pre-plant clean box | Per VM |
| `tz-ready` | End of phase 6, after sweep + hardening + domain | As-delivered competition disk | Per VM |

- Disk-only (`vmstate 0`, fsfreeze via guest agent), constants `SNAP_BASE`/`SNAP_READY`.
- `redeploy-competition.py` modes: `rollback-ready` (default, seconds), `rollback-base` (+ re-run the **post-clone stage** / hardening / domain), `reconfigure` (no rollback, re-run config), `rebuild` (re-clone — from the **golden template** on pipeline-v2 ranges, linked and seconds-fast), `resync` (align engine-authoritative secrets + re-set box passwords, no rollback), `engine-recovery` (re-clone the engine VM from the engine template; fresh empty scoring DB). See [usage-agents.md](usage-agents.md#redeploy-modes-cheapest-first).
- The golden `tz-base` is the rollback guard for a failed/interrupted golden plant (pre-conversion only — templates can't be rolled back).
- Requires a snapshot-capable datastore: ZFS, LVM-thin, Ceph, or qcow2 on file storage. Thick LVM cannot snapshot; deploys there fall back to `reconfigure`/`rebuild`.

## Nakon contract

```
vulndb-ui (catalog) ── read by ──> nakon ── submodule + CLI ──> tezcatlipoca (this repo)
                                                            │
                                                            └── builds a bundle, ships it to the
                                                                scoring engine, runs `nakon deploy` there
```

This repo consumes nakon only as a CLI, run with `cwd = vendor/nakon` so it reads
`vendor/nakon/.env`. It does **not** touch the vulndb directly (no MySQL), does **not** import
nakon internals, and does **not** depend on vulndb-cli (no catalog CRUD/attachments). Scoring is
**Quotient** (cloned to the engine), not huitzilopochtli:

- `nakon randomize --platform <linux|windows> --services N --vulns N --exclude <slow> --source auto --json` → `{services, vulns}` per box type; called from `nakon_ops._nakon_randomize`. Budgets: `ceil(difficulty/3)` services, `difficulty` vulns, excluding `splunk`/`roundcube`.
- `nakon build --config <abs-path> --out bundles --json` → `{bundle_id, path, cached, plans, machines}`; content-addressed under `vendor/nakon/bundles/` (cache hit when catalog unchanged; shared across competitions).
- `nakon deploy --bundle ... --config ... [--only ...] [--strict] [--jobs N]` — executed on the scoring engine after `scp` of the `nakon` package, bundle, and stage config to a **per-run** `/tmp/nakon-<tag>` → `/opt/nakon/<tag>/`; `--only` scopes deploy without changing bundle content; `--jobs` plants machines in parallel (per-machine work is atomic, so each machine's step order is preserved); staging dirs are created and removed only by the run that owns them.

Pipeline v2 runs nakon as **three passes** (see Data flow): pass 1 plants the golden stage on the golden set, strict, before template conversion; pass 2 (phase 5) plants the repair stage (sshd/sudoers) post-clone before domains; pass 3 (phase 6) plants the final stage (disruptive + boot-hostile) after domains, lenient, with `--jobs`. Each pass is its own config + content-addressed bundle; operator-side bundle builds serialize on a lock while deploys run in parallel. Every bundle is linted at build time for undeclared `$VAR` references (see `REQUIRED_VARS` in constants.py).

No in-process `import nakon` and no direct MySQL connection; `VULNDB_UI_URL` or `vendor/nakon/.env`
supplies catalog access at build time only. The bundle carries no credentials. `redeploy-competition.py`
reaches library symbols through `pipeline_api` (`box_platform` → `pipeline_api.os_to_platform`).

`vendor/nakon` is pinned to a release tag; bump it deliberately (`cd vendor/nakon &&
git checkout vX.Y.Z`, then commit the new submodule pointer). Don't develop nakon inside this
checkout — work in the nakon repo, tag a release, then pin it here.

## Secrets

Per-competition secrets are generated fresh in `deploy()` and persisted to
`competitions/<id>/credentials.txt` (0600, human-readable) and
`competitions/<id>/.deploy_state.json` (0600, gitignored, machine-readable):

- `box_password` — `box_username` login on every team box (nakon `machines[].password`).
- `box_creds` — `{"admin": ..., "user1": ..., "user2": ...}` — `linux.credlist` on the engine and OS/DB accounts on each box; the two must agree or auth checks fail.
- `admin` / `inject` / `postgres` / `redis` passwords — Quotient admin/inject logins and internal Postgres/Redis; written to `/opt/quotient/.env` consistently from the same run (`bootstrap_scoring_engine` and `push_event_conf` share the same values).

`.deploy_state.json` also checkpoints `last_phase` and gates
`seeded`/`engine_unpaused`/`injects_created` for idempotent resume (`--from-phase`); the
checkpoint is enforced, not just recorded: a `--from-phase N` that skips a phase the state
file never saw complete is refused, and `--force-from-phase` is the deliberate override. It
carries `pipeline_version` (cross-version resumes are refused) and `deployed_endpoint` +
`scoring_vm_id` (the stale-state guard: a per-comp terraform state from another host or
engine vmid refuses to run rather than letting terraform reconcile it). It also stores
`golden_template_ids` after phase 4. `.env`
(gitignored; `.env.example` is the committed template) holds `TF_VAR_*` and is updated in place by
`config_ops.update_env`. `teams.json`, `nakon-config.json`, `event.conf` are also gitignored.
`boxes.json`, `box_services.json`, `Compfile` are not secret; `vendor/nakon/.env` carries vulndb
credentials for build-time only and never ships to the engine. `.gitignore` keeps every per-run
file out of git: `teams.json`, `event.conf`, `linux.credlist`, `cloned_vms.json` (legacy
pre-golden path), `credentials.txt`, `.deploy_state.json` (the resume checkpoint holding those
secrets), `placement.json` (multi-node), `.frozen.json` (freeze record),
`nakon-config.json` (placeholder creds today, kept out for consistency with its siblings),
`.nakon-domain-*.json` (single-machine nakon configs written per ADDS/Domain-Join step — same
per-run-secret class), `.nakon-golden.json` / `.nakon-repair.json` / `.nakon-final.json` /
`.nakon-postclone.json` (the stage-split configs — they carry `box_password` like every machine
list), `.template-hashes.json` (template content hashes + build inputs),
`.deploy-timings.jsonl` (op names and durations only, no secrets), and `.postclone-swept`
(sweep marker). Ad-hoc `logs/` and `deploy-*.log` are excluded on the same precedent.

## State files

Four per-competition JSON files carry state between runs. All live in
`competitions/<id>/`; schemas below are the fields the code actually reads/writes (see each
module's source for the authoritative list).

| File | Owns | Key fields |
|---|---|---|
| `.deploy_state.json` (0600) | The resume contract + every per-run secret | `last_phase`, `pipeline_version`, `scoring_vm_id`, `deployed_endpoint`, `multi_node`, `teams`, `admin_password`, `inject_password`, `postgres_password`, `redis_password`, `box_password`, `box_creds`, `domain_creds`, `engine_template_vmid`, `engine_template_hash`, `engine_build_info`, `golden_template_ids`, `golden_ids_by_slot`, `golden_hashes`, `plant_coverage_failed`, `nakon_failed_steps`, `seeded`, `engine_unpaused`, `injects_created` |
| `.template-hashes.json` (0600) | M4 reuse gate: content hash + hash inputs per template | `engine` = `{hash, inputs}`, `golden` = `{<box name>: {hash, inputs}}`, plus an `updated` timestamp |
| `.frozen.json` (0600) | Freeze record for a verified competition | `frozen_at`, `code` (git commit info), `hashes` = `{engine, golden}`, `verify_report` = `{gates, plant_coverage}`, `windows_domain_validated` |
| `placement.json` | Multi-node placement (absent = single node) | `version`, `comp`, `engine_vmid`, `engine_node`, `nodes` (per-host records), `slots`, `team_nodes`, `team_slots`, `team_identifiers`, `satellites[]` (`name`, `slot`, `teams`, `jump_vmid`, `jump_mgmt_ip`, `anchor_identifier`), `jump_mgmt_ips`, `probe_summary`, `computed_at`; see [multi-node.md](multi-node.md) |

Gitignored marker files also gate resume: `.postclone-swept` (phase 5 done — the only
sweep marker; `run-agent-scrim.py` skips this name too, so a swept template cannot leak the
marker into a fresh scrim competition), `.nakon-domain-<team>-adds.json` / `-ad-misconfigs.json` / `-ad-accounts.json`
(AD chain progress), and `.nakon-golden-slot<N>.json` (per-slot golden configs). `.deploy_state.json` is written
atomically by `config_ops.write_state` (`write_text_atomic` + 0600-at-creation) because it holds the only copy of the
box passwords.

## Operational invariants

- `terraform/main.tf` and `range_ops.vm_id_for` must agree on the `200 + identifier*10 + index`
  stride; changing one without the other creates collisions. `vm_id_for` is the sole derivation
  point, and targets are built from the full lists then filtered (see Target abstraction).
- **A resume must reuse the original per-run secrets.** The engine's already-written `.env`, the
  already-seeded admin login, and the boxes' passwords were minted on the first run; regenerating
  on resume silently desyncs the deployed range from its credentials. `deploy.py` refuses to
  resume without `.deploy_state.json` rather than risk it.
- `destroy-competition.py` restores the per-competition `TF_VAR_*` values before destroying:
  `for_each` over `var.teams`/`var.boxes_per_team` must produce the exact resource keys of the
  original apply, or resources are orphaned.
- `terraform/main.tf` looks team1 up **by name** — the pipeline hard-assumes a team literally
  named `team1`; a name lookup fails fast where sort order would build the wrong team.
- `event.conf` reaches the engine **before** nakon runs, and phase-7 sub-steps are gated
  individually on `.deploy_state.json` flags because `unpause_engine` is not idempotent.
- On fresh clones the NOPASSWD sudoers grant lands **before** the DNS fix (whose `sudo` calls
  soft-fail until the grant does); the guest-agent fallback covers any remaining gap.
- A fresh deploy must never inherit a previous deploy's `.postclone-swept` marker; the marker is
  written only after a clean sweep so mid-phase-5 resumes don't re-run the sweep.
- **The golden plant is the only strict nakon pass, the boot smoke proves it boots, and `qm
  template` is the checkpoint.** A golden plant that isn't green must not convert, and a golden
  whose throwaway clone did not reach multi-user must not convert either (unverifiable counts as
  failure unless `golden_boot_smoke 0` explicitly waives it); a partially converted golden set has
  no resume (destroy and redeploy). Linked clones inherit whatever the golden disk carries —
  including mistakes.
- **Destroy order: linked clones before golden templates.** Clones depend on the template's
  base disk; deleting a template with live clones orphans them. Phase 1 runs two waves,
  `destroy-competition.py` destroys golden templates after `terraform destroy`.
- **Golden reuse is per-competition only.** A golden set built for competition A can never
  serve competition B (tags `comp-a`, per-competition hash inputs — B's phase 1 would
  rebuild it anyway), so once a run has achieved its goal the golden range is torn down
  with `--full` rather than left on the datastore "for reuse". Teams-only teardown is a
  mid-run mode, not an end state.
- **Never deploy over a per-comp terraform state from another host or engine vmid** — the
  stale-state guard refuses; terraform would otherwise "reconcile" old state and destroy
  whatever now sits at the old vmid on the new host (live-confirmed on realm, 2026-09-24).
- The competitor packet is intentionally the same document for every team and intentionally
  omits `box_vulns.json` — planted misconfigs would spoil the competition.
- Pinned `box_services.json`/`box_vulns.json` make re-runs deterministic and enable `bundles/`
  cache hits across competitions with identical selections.
- All `wait_for_*` helpers are poll-based with timeouts; deploy aborts only when every target of
  a phase is unreachable — that reads as systemic, not a timing fluke. Per-box work runs
  concurrently with independent budgets; the all-fail rule is what distinguishes systemic from
  one box being slow.

## Timeouts & parameter rationale

| Parameter | Value | Why |
|---|---|---|
| `terraform apply` parallelism | `1` | Concurrent full clones saturate the datastore/API (HTTP 596, pvestatd hangs). Linked clones (apply #2) are metadata work, but parallelism stays 1 — they're seconds each anyway |
| Apply #1 timeout | `2400`s | Engine 40 GB full clone + bridges only (team boxes moved to apply #2) |
| Apply #2 timeout | `600 + 300s` per box | Linked clones are seconds; the budget covers per-box API/cloud-init waits |
| Proxmox task wait | 1800 s | Slow storage — clones/deletes can exceed 10 min on this host |
| Engine `compose up` | 600 s | Cold-start runs Postgres initdb; measured >60 s twice (e2e-2026-09-19) |
| ADDS promotion budget | 20 min | Domain promotion is slow |
| Cleanup worker pool | 8 | Deletes are metadata-light; the 596 hazard belongs to bulk clone writes, which stay serial |
| Golden-build per-box pools | 4 | snapshot / password / cloud-init-clean / convert are per-box Proxmox work — the validated per-box bound (same as the Windows-bootstrap and `tz-base`/`tz-ready` pools) |
| Satellite jump-VM builds | 4 | Independent hosts over the direct API path; the serial loop measured 17–547 s per satellite (avg ~150 s, multinode-spread-2026-09-30) |
| Golden boot-smoke timeout | 1200 s Linux / 1800 s Windows | Generous on purpose: it only costs wall clock on a golden that is genuinely failing (a healthy clone returns as soon as it answers), and a fresh clone's first boot can crawl on an overprovisioned thin pool |
| verify probe pools | 8 | Pure SSH / guest-agent probes with no Proxmox task (domains / misconfig-survival / beacons), within sshd's raised `MaxSessions` 64 |
| Per-box wait pool | 8 (≤ sshd `MaxSessions` 64) | Concurrent waits/fixes with independent per-box budgets |
| `range-firewall.timer` | 30 s | Re-asserts NAT + isolation against Docker's iptables wipes |
| `range-healthcheck.timer` | 60 s, offset from :00/:30 | Stays off range-firewall's cadence so the two never fire in the same tick |

## Security rationale

- **All passwords are generated fresh per competition.** This repo is public; the fixed literals
  it once shipped (`ubuntu/ubuntu`, `admin/changeme123`) are guessable from the repo itself or
  by fingerprinting any past deploy.
- **`null_resource` triggers carry identifiers only** — `var.teams` values in triggers would
  print team passwords in every `terraform plan`.
- **The competitor packet omits `box_vulns.json`** — including planted misconfigs would spoil
  the exercise.
- The 2026-08-06 git-history secret disclosure is recorded in
  [security-disclosures.md](security-disclosures.md).
