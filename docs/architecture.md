# Architecture

`create-competition.py` (→ `deploy.py` → `deploy_lib/`) builds a per-competition scoring range in
eight phases: generate Quotient config and a nakon machine list, provision Proxmox with Terraform,
bootstrap the scoring engine, plant services and misconfigurations with nakon. Quotient scores; nakon
provisions. The engine is the only host with a NIC on every team bridge: SSH jump host, nakon
execution host, NAT gateway. Team isolation is iptables on the engine, not bridge separation.

There is one pipeline version. The deploy and `redeploy-competition.py` refuse a
`.deploy_state.json` with a different `pipeline_version`; teardown refuses state without a `run_id`.

Per-symbol rationale: [internals.md](internals.md). Open problems: [known-issues.md](known-issues.md).

## Layout

| Path | Purpose |
|---|---|
| `*-competition.py`, `compile-packet.py`, `sync-template.py`, `generate-packet.py`, `run-schedule.py` | Thin CLIs. Hyphenated, so not importable; `pipeline_api.py` is the import surface for `redeploy-competition.py` |
| `deploy_lib/` | Deploy pipeline: `cli`, `runner` (`deploy()`), `prepare`, `context`, `placement`, `tfinputs`, `golden_plan`, …; `phases/` holds the eight phase functions |
| `infra/` | Everything that touches Proxmox: API `client`, `guest_exec`, `lifecycle`, `ownership`, `console`, `gates` (all pre-flight/ownership/state-version refusals, `run_preflight`) |
| `nakonops/` | Nakon surface: config generation, pin validation, bundle build, engine flock, `run_nakon` |
| `golden/` | Golden set: build, plant, convert to template, slot arithmetic, disk sizing |
| `engine/` | Engine template lifecycle (hash, freeze, build) and engine bootstrap/identity/health/event steps |
| `guests/` | Per-box hardening, AD forests (`domain_ops`), Alpine/Ubuntu/apt shims |
| `multinode/`, `net/` | Placement, satellite jump VMs, template sync, in-path pfSense firewalls |
| `redeploy/`, `teardown/`, `verifier/` | Redeploy modes, teardown engine, post-deploy gate package |
| `quotient/setup.py` | `event.conf`, team seeding, unpause, injects; `_SERVICE_TO_CHECK` map |
| `common/` | `constants` (vmid math, limits, snapshot names), `targets`, `ssh_ops`, `windows_ops`, `timing`, `utils` |
| `terraform/` | Team and transit bridges, engine, every team box as slot-dimensioned resources |
| `red/`, `run-agent-scrim.py`, `scrim-report.py` | Red/blue agent scrim ([scrim-harness.md](scrim-harness.md)) |
| `vendor/nakon` | Submodule pinned to a release; CLI-only catalog access and bundle builder |

## Phases

`--from-phase N` needs state from the same pipeline version, and is refused if the state never saw
the earlier phases complete (`--force-from-phase` overrides).

1. **Reclaim + local reset** — a deploy destroys nothing a run owns. A leftover range (team boxes,
   engine, jump VMs, bridges) is a preflight *refusal* naming `destroy-competition.py`; this
   competition's own templates are kept and adopted (phase 2/4 reuse them by hash, rebuilding only on
   drift). Phase 1 itself reclaims the untagged interrupted clones carrying this competition's clone
   marker, resets `.nakon-domain-*` / `.postclone-swept`, and clears the previous teardown's journal.
   Skipped when `--from-phase > 1`.
2. **Engine template + apply #1** — compute the engine template hash (base image vmid, pinned
   `quotient_ref`, bootstrap/`main.tf` code); reuse on match, rebuild on drift (never when frozen:
   config drift fails, code drift warns). Rebuild = full-clone base, bootstrap, clean (containers,
   volumes, `.env`, host keys, machine-id, cloud-init), `qm template`. Then `terraform apply
   -parallelism=1` with `build_team_boxes=false` creates the engine as a linked clone, the team
   bridges, the engine's team NICs (netplan) and a cold boot. Multi-node: per-satellite jump VMs
   (pool of 4), then `routing_ops` gates engine→jump reachability.
3. **Prepare engine** — write `.env` before `compose up` (fresh Postgres volume, empty scoring DB every
   run), push `event.conf`/`linux.credlist`, re-assert NAT.
4. **Golden set + apply #2** — full-clone one VM per box type onto team1's subnet at `.240+`, plant the
   golden stage (strict, under the `tz-base` rollback guard), clean cloud-init, `qm template` (hash on
   the template description and `.template-hashes.json`). Then `apply` with `build_team_boxes=true`
   creates every team box as a linked clone. Windows bootstrap, SSH/cloud-init waits,
   `setup_ubuntu_auth`, DNS fix, apt prep, `tz-base` snapshots. Records the plant-coverage verdict
   (`{box}-golden`, or `{box}-golden-slot{N}`) in `.deploy_state.json`.
5. **Firewall bootstrap** — only when the lineup has an `unmanaged` + `in_path` box. Generate one
   pfSense `config-team<id>.xml` per team from `pfsense/pfsense-config-orig.xml`, push over SSH via
   the engine (clones boot as 192.168.1.1), wait for SSH on the WAN address, then cut the engine
   over: drop `192.168.<id>.1`, add `192.168.<id>.0/24 via 172.31.<id>.2`. A gate proves every team
   subnet routes through its firewall. A later `terraform apply` rewrites the pre-cutover netplan
   ([known-issues.md](known-issues.md)).
6. **Pre-domain pass** — plant the pre-domain stage (sshd/sudoers touchers) on every team box
   (lenient, `--jobs`), then `fix_services_on_boxes` on Linux boxes only. Failures are recorded per
   machine. `.postclone-swept` lets a resume skip it.
7. **Domains → final pass → beacons → `tz-ready`** — per-team AD forests concurrently (serial within a
   team), then the final stage (disruptive DNS/apt and boot-hostile configs) after the joins, beacons,
   and the `tz-ready` snapshot (pool of 4). The firewall's `tz-base` lands at the end of phase 5, after
   cutover.
8. **Seed** — poll Quotient, `seed_teams`, `unpause_engine`, `create_injects`, each gated on
   `.deploy_state.json` flags. Opt-in (Compfile `portal 1`): the student portal ([portal.md](portal.md)).

## Data flow

```
Compfile + boxes.json
  → box_services.json / box_vulns.json   (randomized via nakon, or pinned; per box type)
  → nakon-config.json                    (per machine: ip/user/password/configurations)
  → generate_stage_configs:
      .nakon-golden.json     one machine per box type, golden stage     pass 1, strict, phase 4
      .nakon-predomain.json  every team machine, sshd/sudoers touchers  pass 2, lenient, phase 6
      .nakon-final.json      every team machine, disruptive/boot-hostile pass 3, lenient, phase 7
      .nakon-postclone.json  pre-domain ∪ final; replayed by redeploy rollback/reconfigure
  → content-addressed bundles (vendor/nakon/bundles/, cached by config hash)
  → engine /opt/nakon/<run-tag>/  (`nakon deploy --bundle --config [--only] [--strict] [--jobs N]` over SSH)
```

- `Compfile` (`name`/`scenario`/`difficulty`) and `boxes.json` (`name`/`template`/`cpu`/`memory_mb`/`disk_gb`/`last_octet`) are the only required inputs; `users.json` and `domain_roles.json` are optional.
- `box_services.json`/`box_vulns.json` are per box type, so every team defends the identical set and Quotient wildcard checks (`192.168._.<octet>`) hold.
- Stage split: linked clones inherit the golden disk, so identity-free, non-disruptive configs plant once per box type. The rest splits by ordering: **pre-domain** must precede the domain pass (member joins authenticate over SSH, which a clone's fresh cloud-init can disturb); **final** must follow it (joins reboot members and need working DNS/apt). Membership: `constants.PREDOMAIN_STAGE_CONFIGS` / `FINAL_STAGE_CONFIGS`; `POST_CLONE_CONFIGS` is the union.
- Bundles are built operator-side; the engine never sees vulndb credentials. `--only` scopes a deploy without changing bundle content.

## Targets

`targets.enumerate_targets(teams, boxes)` yields one entry per `(team, box)`:

- `vmid = 200 + identifier*10 + box_index`, mirrored in `terraform/main.tf`; `vm_id_for` is the sole derivation.
- `ip = 192.168.<identifier>.<last_octet>`; `vm_name` is `team1-<box>` or `<identifier>-<box>`; nakon machine name is `<box>-team<identifier>`.
- Limits: `MAX_BOXES_PER_TEAM=10`, `MAX_TEAMS=154` (identifiers 101–254).

Enumerate from the full lists, then filter. vmid is positional; re-enumerating a filtered list assigns wrong IDs.

## Windows

Linux boxes use cloud-init (user, key, password, DNS 8.8.8.8, IP). Windows boxes skip it (template name contains `win`, case-insensitive, in both `os_to_platform` and `main.tf`).

- `bootstrap_windows_box` (guest agent, base64 PowerShell) sets static IP/gateway/DNS, the `Administrator` password, and enables `sshd` and QEMU-GA with a port-22 rule. It is the only channel before networking and credentials exist.
- Templates are sysprepped (`/generalize /oobe /shutdown`, `<ComputerName>*</ComputerName>`) for a fresh hostname/SID per clone.

Domains (`domain_roles.json`, `{"dc01":"dc","member01":"member"}`), per team:

1. Promote the first `dc` box to forest `team<identifier>.local` (`ADDS`, `dsrm_password = box_password`). DC box types use an unbooted golden so each clone specializes its own SID.
2. Wait for guest agent, `sshd`, AD Web Services, then DNS SRV (`_ldap._tcp.dc._msdcs.<domain>`).
3. Join members: Windows via `Add-Computer` after repointing DNS; Linux via nakon `domain-join` (realmd/sssd). Membership is probed; transient failures retry 3×.

Each rebooting step is its own single-machine nakon pass. A missing `domain_roles.json` skips domains; an invalid one (unparseable, role outside `dc`/`member`, or box not in `boxes.json`) is an error in `golden_target_ops.unbooted_golden_boxes` and `verifier.domains.check_domains`.

## Network and isolation

- Team bridges `vmbr<identifier>` have no physical ports. The engine has a virtio NIC per bridge plus `vmbr0`; team NICs are configured via `/etc/netplan/60-team-ifaces.yaml` (`ens19`+, `192.168.<id>.1/24`) after a cold boot so new NICs enumerate.
- Engine iptables: `FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP` (inserted at line 1); `POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE`.
- Docker wipes `FORWARD` on every restart, so `bootstrap_scoring_engine` installs `range-firewall.{sh,service,timer}` (after `docker.service`; 30 s) to re-assert both rules, and `range-healthcheck.{sh,service,timer}` (60 s, offset from :00/:30) logging to `/var/log/range-healthcheck.log`. `ensure_nat_forwarding` re-asserts at phase 3 and on redeploy.

Red identity is owned by `../bad-auto` (`badauto/deploy/engine_nat.py`, `deploy.red_mode`), installed as a `bad-auto-firewall` unit + 30 s timer:

- **routed** (default): red01 holds a secondary address on a dedicated segment (default `10.200.0.0/24`, gateway `.1` added by `ip addr add` in the re-assert script, not netplan). Attacks keep the red source end-to-end; scoring still sources from the team gateway.
- **masq**: red01 is MASQUERADEd into the team gateways, indistinguishable from scoring.

Both: `FORWARD ACCEPT red→192.168.0.0/16`, ESTABLISHED accept ahead of the DROP, `FORWARD DROP` toward red. Beacon C2 is a DNAT from the team gateway to red01. `verify-competition.py --red-identity` proves the routed source survives.

## Snapshots

| Snapshot | When | Scope |
|---|---|---|
| `tz-base` | After boot/auth/DNS/apt prep, before plant (golden boxes) or before the post-clone sweep (team boxes) | Per VM |
| `tz-ready` | End of phase 7, after sweep, hardening, domains | Per VM |

Disk-only (`vmstate 0`, fsfreeze via guest agent); constants `SNAP_BASE`/`SNAP_READY`. The golden `tz-base` guards a failed golden plant (pre-conversion only; templates cannot roll back). Needs ZFS, LVM-thin, Ceph, or qcow2 on file storage; thick LVM falls back to `reconfigure`/`rebuild`. Redeploy modes: [usage-agents.md](usage-agents.md#redeploy-modes-cheapest-first).

## Nakon contract

Nakon is consumed as a CLI with `cwd = vendor/nakon` (reads `vendor/nakon/.env`). No `import nakon`, no direct MySQL, no vulndb-cli. Catalog access is build-time only; bundles carry no credentials.

- `nakon randomize --platform <linux|windows> --services N --vulns N --exclude <slow> --source auto --json` → `{services, vulns}`. Budgets: `ceil(difficulty/3)` services, `difficulty` vulns, excluding `splunk`/`roundcube`.
- `nakon build --config <abs> --out bundles --json` → `{bundle_id, path, cached, plans, machines}`; content-addressed, shared across competitions; operator-side builds serialize on a lock.
- `nakon deploy --bundle … --config … [--only] [--strict] [--jobs N]` runs on the engine after `scp` to a per-run `/tmp/nakon-<tag>` → `/opt/nakon/<tag>/`. `--jobs` parallelizes across machines; per-machine step order is preserved. Staging dirs are removed only by the run that created them. The engine must already have paramiko (`python3-paramiko`, baked into the engine template) — the deploy probes and refuses by name rather than installing into the engine's python.

`vendor/nakon` is pinned to a release tag. Bump with `cd vendor/nakon && git checkout vX.Y.Z` and commit the pointer; develop nakon in its own repo.

## Secrets and state

Per-run secrets are generated in `deploy()` and written to `competitions/<id>/credentials.txt` (0600) and `.deploy_state.json` (0600, atomic via `write_state`; the only copy of the box passwords):

- `box_password` — `box_username` login on every box.
- `box_creds` — `admin`/`user1`/`user2`; must match `linux.credlist` on the engine.
- `admin`/`inject`/`postgres`/`redis` — Quotient and internal DB logins, written consistently to `/opt/quotient/.env`.

`.env` (gitignored; `.env.example` is committed) holds `TF_VAR_*` inputs and is never rewritten. `vendor/nakon/.env` holds vulndb credentials. Everything per-run is gitignored: `teams.json`, `event.conf`, `linux.credlist`, `credentials.txt`, `.deploy_state.json`, `placement.json`, `.frozen.json`, `nakon-config.json`, `.nakon-*.json`, `.template-hashes.json`, `.deploy-timings.jsonl`, `.postclone-swept`, `logs/`, `deploy-*.log`. `boxes.json`, `box_services.json`, `Compfile` are not secret.

| File | Owns | Key fields |
|---|---|---|
| `.deploy_state.json` | Resume contract + secrets | `last_phase`, `paused_after_phase` (present only while a run is held at a boundary), `pipeline_version`, `run_id`, `scoring_vm_id`, `deployed_endpoint`, `multi_node`, `teams`, the password fields, `box_creds`, `domain_creds`, `engine_template_{vmid,hash}`, `engine_build_info`, `golden_template_ids`, `golden_ids_by_slot`, `golden_hashes`, `plant_coverage_failed`, `nakon_failed_steps`, `seeded`, `engine_unpaused`, `injects_created`, `portal_*` |
| `.template-hashes.json` | Reuse gate | `engine` = `{hash, inputs}`, `golden` = `{<box>: {hash, inputs}}`, `updated` |
| `.frozen.json` | Freeze record | `frozen_at`, `code` (git), `hashes`, `verify_report` |
| `placement.json` | Multi-node placement (absent = single node) | See [multi-node.md](multi-node.md) |

`deployed_endpoint` + `scoring_vm_id` are the stale-state guard. Marker files: `.postclone-swept` (removed by phase 1 and phase 4, written only after a clean sweep; `run-agent-scrim.py` skips it), `.nakon-domain-<team>-*.json` (AD chain progress), `.nakon-golden-slot<N>.json`.

## Invariants

- `terraform/main.tf` and `vm_id_for` must agree on the vmid stride.
- A resume reuses the original per-run secrets; `infra.gates` refuses to resume without `.deploy_state.json`.
- `destroy-competition.py` restores the per-competition `TF_VAR_*` first: `for_each` over `var.teams`/`var.boxes_per_team` must reproduce the original keys or resources orphan.
- `main.tf` looks team1 up by name; the pipeline assumes a team literally named `team1`.
- `event.conf` reaches the engine before nakon runs. Phase 8 sub-steps are flag-gated because `unpause_engine` is not idempotent.
- On fresh clones the NOPASSWD sudoers grant lands before the DNS fix.
- The golden plant is the only strict pass and `qm template` is the checkpoint. A non-green plant must not convert; a partially converted set has no resume (destroy and redeploy). Clones inherit golden mistakes.
- A deploy never tears down a range: a leftover team box/engine/bridge is a preflight refusal and `destroy-competition.py` reclaims it. Linked clones still die before their golden templates — that ordering lives in teardown (`destroy-competition.py` destroys goldens after `terraform destroy`).
- Golden sets are per-competition; tear them down when the run's goal is met. Teams-only teardown is a mid-run mode.
- Never deploy over a per-comp terraform state from another host or engine vmid; terraform would reconcile and destroy whatever now sits at the old vmid. The stale-state guard refuses.
- The competitor packet omits `box_vulns.json` (it would spoil the exercise).
- Pinned `box_services.json`/`box_vulns.json` make reruns deterministic and enable bundle cache hits.
- `wait_for_*` helpers poll with timeouts; a phase aborts only when every target is unreachable (systemic), not when one box is slow.

## Timeouts and parameters

| Parameter | Value | Why |
|---|---|---|
| `terraform apply` parallelism | 1 | Concurrent full clones saturate datastore/API (HTTP 596, pvestatd hangs) |
| Apply #1 timeout | 2400 s | 40 GB engine full clone + bridges |
| Apply #2 timeout | 600 s + 300 s/box | Linked clones; budget covers API/cloud-init waits |
| Proxmox task wait | 1800 s | Slow storage |
| Engine `compose up` | 600 s | Postgres initdb cold start |
| ADDS promotion | 20 min | |
| Cleanup pool | 8 | Deletes are metadata-light; bulk clone writes stay serial |
| Golden per-box pools, jump-VM builds, snapshot pools | 4 | Per-box Proxmox work bound |
| verify probe pools, per-box wait pool | 8 | SSH/agent probes, ≤ sshd `MaxSessions` 64 |

## Security

- All passwords are generated per competition; the repo is public, so fixed literals are guessable.
- `null_resource` triggers carry identifiers only; team passwords in triggers would print in every `terraform plan`.
