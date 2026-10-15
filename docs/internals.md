# Internals — design notes

The "why" behind symbols, constants, orderings, and workarounds. System-level material (phases, data flow, isolation, state schemas, nakon contract) is in [architecture.md](architecture.md) and not repeated. The scrim harness is in [scrim-harness.md](scrim-harness.md). Re-verify timeouts and symbol names against the tree before relying on them.

## common/constants.py

Single source for vmid math, snapshot names, nakon budgets, naming.

| Symbol | Value | Why |
|---|---|---|
| `SCORING_ENGINE_VMID` | 1000 | Default only; `--scoring-vmid` / `TF_VAR_scoring_vm_id` overrides. 100 collides with an unrelated VM on the primary node |
| `DEFAULT_ENGINE_MGMT_IP` / `_GW` | `10.0.0.250` / `10.0.0.1` | Static: a DHCP engine can reboot onto a new address while terraform's saved output stays stale |
| `MAX_BOXES_PER_TEAM`, `MAX_TEAMS` | 10, 154 | Stride-10 vmid block per team; identifiers 101–254 |
| `SNAP_BASE` / `SNAP_READY` | `tz-base` / `tz-ready` | |
| `PER_MACHINE_NAKON_BUDGET` | 2400 s | `run_nakon` timeout is `max(2400, budget × machines)` |
| `SLOW_SERVICES` | `splunk`, `roundcube` | Excluded from auto-randomize |
| `DISRUPTIVE_CONFIGS` | `resolv-conf-null-dns`, `apt-sources-empty`, `apt-hold-all-packages`, `dpkg-broken-hold-state` | Break DNS/apt; sort last in a pass |
| `DOMAIN_INFRA_CONFIGS` | `ADDS`, `Domain Join`, `domain-join` | Scored but never planted from `box_services.json`: `domain_ops` injects them per team with domain/DSRM vars a golden plant can't carry |
| `PIN_CHECK_OVERRIDES` | `display`, `port`, `path`, `scheme`, `status`, `credlist` | Per-pin scoring overrides for dict pins. Quotient needs unique `<box>-<Display>`, so `display` lets same-type pins coexist. Stripped from nakon machine lists |
| `PREDOMAIN_STAGE_CONFIGS` / `FINAL_STAGE_CONFIGS` / `POST_CLONE_CONFIGS` | sets | Stage membership ([architecture.md](architecture.md#data-flow)) |
| `REQUIRED_VARS` | map | Curated (the catalog has no required-vars metadata). `"ip"` = machine identity, auto-filled, banned from the golden stage; `"ip:<box>"` = same team's copy of another box; `"literal"` = operator pins `{"name","vars"}` |
| `GOLDEN_VMID_OFFSET` / `GOLDEN_IP_BASE` / `GOLDEN_TAG` | 150 / 240 / `tezcatlipoca-golden` | Goldens sit above the engine slot; IPs `.240+` on team1's subnet are free because goldens convert before team boxes exist |
| `ENGINE_TEMPLATE_VMID_OFFSET` / `ENGINE_TEMPLATE_NAME` | 140 / `engine-template` | Just below the golden block so one preflight scan covers both |
| `GOLDEN_CLONE_TIMEOUT` | 5400 s | A 60 GB Windows full clone took >30 min on HDD-ZFS |
| `WINDOWS_ADMIN_USER` | `Administrator` | |
| `NAKON_DIR` | `vendor/nakon` | cwd for nakon subprocesses so they read its `.env` |

## common/utils.py, ssh_ops.py

- `DNS_FIX_CMD`: cloud-init ignores `dns.servers` on static IPs, so DNS is repaired live (`/etc/resolv.conf`, `.head`, a `resolved.conf.d/upstream.conf` drop-in, restart `systemd-resolved`; 8.8.8.8). `DNS_FIX_CMD_ROOT` is the same without `sudo`, for the guest-agent path (needs neither sudoers nor network).
- `load_compfile()`: key=value; non-numeric `difficulty` → 0. `compfile_flag()`: integer knobs (`team_beacons 1`), default when absent.
- `engine/round_loop.py` is the one definition of "round loop stopped" (`round_loop_state`/`is_stale`), used by verify.
- `run_concurrent(items, fn, max_workers=8)`: returns an index-aligned list of values or exceptions (never raises). Aggregation: `wait_for_boxes_ssh`/`fix_dns_on_boxes` abort only if **all** boxes fail; `prep_apt_on_boxes`/`fix_services_on_boxes` warn per box; `setup_ubuntu_auth` raises if any box ends without auth (after trying all). Cap 8 is under the engine's sshd `MaxSessions` 64. `fix_services_on_boxes` builds scripts serially and executes in parallel.
- `quote_sshkeys()` (URL-encodes the key for Proxmox's `sshkeys`) is defined once in `ssh_ops`.
- `ssh_via_gateway()` uses `ProxyCommand ssh -W`, requiring `AllowTcpForwarding yes` on the engine. Linux uses the Proxmox key as `box_username`; Windows uses password.
- Engine SSH is multiplexed (`ControlMaster=auto`, `ControlPath=~/.tezcatlipoca/cm-<ip>-22-%r`, `ControlPersist=900`). Coupled requirements: `forget_engine_host_key` must retire the master socket (a surviving master keeps the old host key after a rebuild), and engine bootstrap raises `MaxSessions` to 64 and `MaxStartups 30:30:100`.
- `wait_for_ssh`/`wait_for_http` poll and never raise. `wait_for_boxes_ssh` probes Windows via guest agent and raises only if all fail. `wait_for_cloud_init` must finish before planting (cloud-init can revert perms); Windows skipped; exit 2 = done with warnings.
- `is_windows_template()` (`"win" in name.lower()`) has one definition in `common/windows_ops.py`; every classifier imports it, because divergent copies build the wrong range.

## common/comp_paths.py

Competition definitions moved to their own repo (`tezcatlipoca-competitions`), so "the comp
dir" stopped being a literal `competitions/<id>`. `comps_root()` resolves
`TEZ_COMPETITIONS_ROOT` → `./competitions` (the old CWD-relative idiom, run from a worktree
root) or the checkout's own `competitions/` → the sibling `../tezcatlipoca-competitions`.
`resolve_comp_dir` puts `--comp-dir` ahead of name resolution everywhere: the dashboard runs
drivers against per-run workspace copies (definitions seeded in, run state accumulated
there), and the drivers never learn about the dashboard. Per-run state lives inside whatever
comp dir the driver is given, so a workspace IS a comp dir — that is what keeps manual runs
and dashboard runs on one code path.

## infra/

One submodule per concern; **tests patch the owning module** (`infra.client.proxmox_api`, `infra.guest_exec.time`, `infra.ownership.has_clone_marker`, `infra.lifecycle.list_snapshots`).

| Module | Owns |
|---|---|
| `client` | `proxmox_api[_for]`, `proxmox_request` (TLS pinning, per-record fingerprint), node routes, `cluster_vms_for`, `wait_for_proxmox_task`, `live_vmids`, load throttle, `destroy_bridge_if_exists` |
| `guest_exec` | `guest_agent_exec_root/windows/detached`, `wait_for_guest_agent`, `diagnose_unreachable_box` |
| `ownership` | The ownership proof and every ownership-guarded mutation |
| `lifecycle` | Status/stop/start, snapshots; no ownership decisions |
| `console` | vncproxy sessions and per-comp portal console tokens |
| `gates` | Deploy refusals (`acquire_deploy_lock`, resume gates, range gates) and `run_preflight` |
| `plan`, `collisions`, `resources` | Preflight plan model; vmid/bridge/mgmt-IP collisions; datastore headroom and template checks |
| `common/targets.py`, `common/terraform_workdir.py` | `vm_id_for`, `enumerate_targets`, persisted targets; per-comp terraform dir, `team_vmids_from_state` |

- **Ownership** (`ownership_verdict(node, vmid, raw_tags, expect_tags)`): `owned` (full set), `marker` (no tags + this competition's clone marker), `unproven` (no tags, no marker), `missing` (tags present, set incomplete). `destroy_vm_if_exists` (refuses unless owned/marker) and the preflight collision gate (ours = owned/marker) share it, so teardown and preflight can't disagree.
- **Run ids**: `utils.mint_run_id` mints `run-<8 hex>` once per competition dir (persisted in state, reused by resume/redeploy). Terraform adds `run_tag` to engine and team-box tags; golden/jump/engine-template creation builds it in. The comp tag is shared by every worktree running that competition ID, so destruction identity is the full set. Preflight treats a comp-tagged VM without the run tag as a collision.
- The API runs with `verify=False` by default (same self-signed trade-off as the provider's `insecure = true`); entry points call `urllib3.disable_warnings()`. `proxmox_api()` retries `ConnectionError`/`Timeout` up to 4× (never HTTP errors). Task wait is 1800 s.
- `guest_agent_exec_root`: bash as root over virtio-serial; bypasses broken sudo, needs no network; returns `(rc, out, err)`, raises on agent failure. `guest_agent_exec_windows`: PowerShell as SYSTEM via `-EncodedCommand` (UTF-16LE base64); the only channel into Windows before bootstrap. `wait_for_guest_agent` returns bool.
- `stop_vm`/`start_vm`: ACPI first, forced stop only if ignored (for VMs that survive; teardown force-stops). `take_snapshot` is disk-only, replaces a same-named snapshot, and **never raises** (optional recovery; thick LVM can't snapshot). `rollback_snapshot` needs stop/start and raises. `list_snapshots` returns an empty set when the VM is gone or the API refuses, so callers fall back to a mode needing none.
- Phase-1 cleanup: pool ≤8 over computed team vmids and the engine vmid (deliberately broader than terraform state: a skipped clone would let a resume land on a stale disk with old passwords). Every destroy needs the PRIOR run's ownership set (`ctx.reclaim_tags`). Bridges are removed only when no VM is attached (`_bridge_in_use`).

### Preflight (`infra/gates.py`)

`run_preflight(plan)` is the one gate before Proxmox mutation. A `PreflightPlan` is a list of `NodeShare`s (single-node = one share over the cluster view; multi-node = one per hosting node). Order: remote-access gate (if enabled); per share, template checks, vmid and bridge collisions (via `ownership_verdict`), datastore headroom; then engine and jump mgmt-IP gates, the red01 gate (`infra/red01.py`), and the nakon catalog check. Concurrent deploys aren't warned about: per-resource gates refuse real collisions and `acquire_engine_lock` (`nakonops/nakon_lock_ops.py`) serializes deploys sharing one engine. Call `run_preflight` before any new mutation path.

## config/config_ops.py

Team identity/secrets and persistence; prompts in `config_prompts.py`, injects in `injects_config.py`.

- `list_proxmox_templates()` mirrors `main.tf`'s lookup (the `template` tag alone) and excludes the engine's own template.
- `collect_teams()`: `identifier = 100 + i`; refuses identifiers whose 10-slot vmid block overlaps the engine, engine-template, or golden block.
- `collect_boxes()`: per competition; reuse replays `boxes.json`. Blank `disk_gb` keeps the template's disk (Terraform emits a disk block only when set; Proxmox can't shrink).
- `collect_users_config()`: credlist input must be exactly 3 names, else defaults.
- `resolve_inject_times()`: offsets are minutes from competition start, resolved to RFC3339 in phase 8 so they anchor to the real start.
- **Inject timing**: `rollback-ready` does not reopen injects (offsets stay anchored). `--reset-event` re-clones the engine and reruns the seed, re-anchoring at now; `verify` WARNs on injects already past close. The scrim path calls `scrim.inject_sync.reanchor_injects()` right after the event clock starts, via UpdateInject (`POST /api/injects/{id}`), re-listing every `InjectFileNames` entry because UpdateInject deletes unlisted attachments.

## nakonops/

Nakon is a subprocess with `cwd = NAKON_DIR`. Patch a name on the module whose function uses it.

- `os_to_platform()`: `"windows"` iff `"win"` in the template name; `redeploy`'s `box_platform()` goes through it.
- `_nakon_randomize()`: on failure names the real prerequisites (vulndb reachable, or `VULNDB_UI_URL`; check `vendor/nakon/.env`).
- `generate_nakon_config()`: if either pinned file exists it is honored, and **both files are always (re)written** so later reads don't fail on a half-pinned set. Otherwise one randomization per box type (identical services per team, which Quotient's wildcard IPs require). Budgets: `ceil(difficulty/3)` services (min 1), `difficulty` vulns (min 1). Disruptive vulns sort last (entries may be strings or `{"name","vars"}`). A second pass fills `REQUIRED_VARS` identity vars.
- `generate_stage_configs()`: `.nakon-golden.json` (one machine per box type at `192.168.<team1 id>.<240+i>`, minus the post-clone subset), `.nakon-predomain.json`, `.nakon-final.json`, and `.nakon-postclone.json` (union, for redeploy). `unbooted` types (DCs) get no golden machine; their plants move into the pre-domain stage. Empty subsets are dropped. All carry `box_password`. Multi-node adds `.nakon-golden-slot<N>.json`.
- `build_nakon_bundle()`: content-addressed; same error contract as randomize.
- `run_nakon()`: scp the nakon package, bundle, config to `/tmp/nakon-<tag>`, stage to `/opt/nakon/<tag>/`. Staging is per run, so concurrent runs don't race: each removes only its own dirs, the timeout `pkill -9 -f '/opt/nakon/<tag>'` is scoped to the run, and `pip3 install paramiko` is check-then-install. `only=` scopes machines without changing bundle content. `strict=True` passes `--strict`. `jobs>1` passes `--jobs` (Compfile `nakon_jobs`, default 4); the ceiling is engine egress/CPU and mirror throughput, not the datastore. Redeploy keeps `jobs=1` to stay gentle on live boxes.
- `_run_single_nakon_config()`: one machine, own config/bundle/`--only`, reboot-safe; used by the domain pass.

## engine/ (scoring engine)

`_run_engine_cmd` (`engine_cmd_ops`) is the one remote-step helper; `step=` is required so failures name themselves.

- `bootstrap_scoring_engine()`: Postgres/Redis passwords are generated once per `deploy()` and passed in so its `.env` agrees with `push_event_conf()`. Clears apt locks first; first-boot `unattended-upgrades`/`apt-daily-upgrade` respawn apt after any single killall, so the units are masked and all three locks polled free (`DPkg::Lock::Timeout` doesn't cover the archives-cache lock).
- **apt-cacher-ng** (port 3142): `prep_apt_on_boxes(use_proxy=…)` writes `/etc/apt/apt.conf.d/95tz-proxy` pointing each box at its gateway IP, gated by Compfile `apt_cache` (default on); `use_proxy=False` removes it. Packages cross the mirror once per competition, which lets `nakon_jobs` scale. Trade-off: apt no longer needs box DNS, so `resolv-conf-null-dns` no longer breaks apt.
- Post-Docker: Docker sets `FORWARD` to DROP and wipes rules, so forwarding and isolation are re-asserted immediately, plus `AllowTcpForwarding yes` + sshd reload.
- `install_range_healthcheck()`: logs failures only. The API probe is not `curl -f`: `/api/login` is POST-only, so GET's 405 still proves liveness; only curl `000` is down. Checks containers, API, isolation DROP, NAT MASQUERADE.
- `ensure_nat_forwarding()`: nakon swallows apt failures, so a dropped MASQUERADE makes services silently not install. Covers the gap before `range-firewall.timer` exists (phase 3, redeploy). Only boxes→internet needs NAT; engine→box scoring routes directly. Idempotent (`-C … || -I/-A`).
- `push_event_conf()` takes the per-run passwords and the `box_creds` given to `fix_services_on_boxes()`. **`box_creds` has deliberately no defaults**: a mismatch or stale fallback silently scores healthy boxes as down. Credlist lands at `/opt/quotient/config/credlists/linux.credlist`; Quotient restarts afterward.

## common/windows_ops.py

- `bootstrap_windows_box()`: waits for the agent and **raises** on timeout (everything depends on it). Configures the single `Up` adapter, sets `Administrator` via `net user` (load-bearing: nakon's paramiko uses it), enables `sshd` + `QEMU-GA`, opens :22.
- `dns_repoint_windows_box()`: points DNS at the team DC before a join (`Add-Computer` needs the SRV records).
- `wait_for_windows_sshd()`: agent-up doesn't imply sshd-up after the ADDS reboot. `wait_for_dc_dns()` polls `_ldap._tcp.dc._msdcs.<domain>`. Both never raise.

## Box hardening (`guests/`)

- `wait_boxes_settled()`: polls the actual condition (agent ping, no `unattended-upgr` process, dpkg lock free via `apt-get check` with a 1 s lock timeout) instead of sleeping out the post-boot upgrade storm; ceiling 240 s, then falls through to prep's retry ladder. Once per `prep_apt_on_boxes` call.
- `prep_apt_on_boxes()`: 5 attempts, 15 s apart; `command -v apt-get || exit 0` makes Fedora/Alpine an honest no-op.
- `fix_dns_on_boxes()`: 8 attempts, 15 s apart, then guest-agent root with `DNS_FIX_CMD_ROOT`. All-fail aborts.
- `setup_ubuntu_auth()`: sshd password auth + NOPASSWD sudo for `box_username`; 8 attempts, then guest-agent root.
- `fix_services_on_boxes()` (`box_creds` required, no fallback; script base64-encoded). Always:
  - Creates credlist OS accounts.
  - **Un-wedges sshd**: several catalog configs each `systemctl restart ssh`, and back-to-back restarts trip systemd's start-limit and leave sshd down for the event. `reset-failed` + conditional `start`, over SSH if up, else the guest agent. This is why it runs **after** the pre-domain pass.
  - Per service: MySQL/MariaDB bound to `0.0.0.0` + credlist DB users (`@'%'`, `GRANT ALL`, first also `WITH GRANT OPTION`); Postfix `inet_interfaces = all` and the `smtp/inet` master.cf entry; vsftpd anonymous off, local+write on; nginx restart; Dovecot plaintext auth (the `auth_allow_cleartext` key only on ≥2.4, which renamed it) plus a mail dir per account; bind9 `allow-query { any; }` + forwarders, recreating `named.conf.default-zones`/`db.local` if stripped; telnet via `update-inetd --enable`; Splunk: nakon only creates a user, so lighttpd on 8000 gives Quotient's check a listener; finally start every installed service.
  - If the SSH run exits non-zero (writable-sudoers-type misconfigs break sudo), retry the same script via guest agent as root with `sudo ` stripped by `re.sub(r"\bsudo ", …)`; `\b`, not `^\s*`, so mid-pipeline uses are caught.

## golden/

`golden_target_ops` (slot arithmetic, `golden_targets`, `unbooted_golden_boxes`) and `golden_disk_ops` (sizing, root expansion) are separate; import from the owner. `build_golden_set` sequences `_build_unbooted_goldens`, `_clone_missing_goldens`, `_convert_goldens`.

One VM per box type is full-cloned to `engine_vmid + 150 + box_idx`, planted with the golden stage (strict), cloud-init cleaned, stopped, converted.

- **Why the API, not Terraform**: `qm template` conversion happens outside the VM resource's lifecycle, so a Terraform-managed golden drifts on conversion. Terraform consumes finished templates as `clone { full = false }` sources via `golden_template_ids`.
- **Resume routing**: all golden vmids already templates → skip. Some plain VMs → roll `tz-base`-marked ones back and re-plant, clone the missing. A *partially converted* set is fatal (templates can't be un-templated) unless every converted vmid's description hash matches its expected hash (selective rebuild or added box type); then only unconverted slots are worked. Cold (unbooted DC) slots: any plain VM is a dead attempt that may have booted and specialized, so destroy it and full-clone from the generalized base, then convert without ever booting.
- `unbooted_golden_boxes()`: absent `domain_roles.json` = empty set; present-but-invalid raises. Silently widening the booted set would give the DC a booted shared golden, i.e. duplicate DomainSIDs.
- **Windows identity**: only the original templates are sysprepped, so SIDs duplicate across teams and stay unique within a team; phase 6 never re-syspreps.
- Per-box snapshot/password/cloud-init-clean/convert passes use `run_concurrent(max_workers=4)` (each drives a Proxmox task; 4 not 8). Full-clone loops stay serial.
- **Root expansion** runs guest-side before the plant. A Fedora btrfs root reports `/dev/sda3[/root]`; the bracket breaks the partition-digit parse and resize2fs can't grow btrfs, so expansion strips the suffix, grows btrfs natively, and tolerates a late `/dev/dm-N` (`dmsetup mknodes`). It retries, then fails hard only when the root fs is measurably too small; unmeasurable warns.

## Template lifecycle (`engine/template_freeze.py`, `engine_template_ops.py`)

Per competition: build → test runs (reuse; rebuild on config change) → freeze → competition → destroy. Not a cross-competition cache; templates are never patched in place.

- **Hashes**: sha256 per template over only what affects disk contents, stored on the template description (node-side truth) and in `.template-hashes.json` (survives state resets; 0600, goldens embed `box_password`).
  - *config-class*: base template vmid; golden-stage config list with pinned `{name, vars}`; the per-box `payload_hash` (that box's plan step `script_sha256`s, so a web01 pin change doesn't rebuild app01/dc01/win01); box user/password; SSH key; apt-proxy flag. Engine: base image vmid + pinned `quotient_ref`.
  - *code-class*: disk-affecting function sources (engine: bootstrap + clean step) plus the engine's `scoring_engine` `main.tf` block only, so team-box edits don't rebuild the engine.
  - Excluded: team count/identifiers/IPs, `event.conf`, pre-domain/final configs, snapshot names, mgmt IPs, DB passwords (applied after cloning or cleaned from the template). Going from a 2-team test to an 8-team event rebuilds nothing.
- **Reuse vs rebuild**: the template's own node description (`tezcatlipoca-m4 hash=`) is the authority — stamp matches the current inputs → reuse; differs + not frozen → destroy that template's clones and the template, rebuild; missing → build. Golden granularity is per box type. `.template-hashes.json` corroborates (it carries the recorded *inputs* the frozen gate classifies drift against); it never decides reuse, because it is per-comp-dir state that the dashboard's workspace seeder strips (`workspaces._STATE_NAMES`) — keying on it alone rebuilt every template a `--teams-only` teardown had deliberately kept, which is what made "test, then deploy the same templates" impossible. Every reuse/rebuild decision goes through `template_freeze.template_stamp_current`.
- **Engine template build**: full clone of the base image → bootstrap at pinned `quotient_ref` (the hash input must be computable before building; the realized HEAD is recorded for traceability only) → clean (compose down -v, remove `.env`/`event.conf`/credlists, truncate machine-id, cloud-init clean, remove host keys; apt-cacher-ng stays installed with an empty cache) → `qm template`. The deployed engine is a linked clone with fresh identity and host keys every run.
- **Frozen**: never rebuilds. Config-class drift hard-fails naming the fields; code-class drift warns and proceeds off the frozen template (a post-freeze log line mustn't break a mid-event rebuild or recovery). For goldens, the pre-phase-1 `golden_freeze_gate` refuses config drift before any Proxmox mutation and warns on code-only drift; phase 4's rebuild gate applies the same verdict before destroying a template. Mid-event rebuilds/recoveries warn when the node's hash differs from the verified record.
- **Freeze** (`verify-competition.py --freeze`): requires all gates PASS incl. plant coverage; records hashes, git commit, dirty flag, timestamp, gate results in `.frozen.json`. `--unfreeze --confirm-unfreeze` is pre-competition only.
- **Full teardown** also removes `.template-hashes.json`; a surviving record would "reuse" a hash with nothing behind it.
- **Team rebuild** re-clones from the goldens and runs the post-clone stages in deploy order (pre-domain → domains → final), never the full config; planting boot-hostile configs before the joins is the brick hazard the split removed.
- **Engine recovery**: `terraform apply -replace` on the engine, with `-target` so terraform can't touch `team_box` (a drifted box, which `rebuild` causes by design, would otherwise be destroyed mid-event).

## guests/domain_ops.py

Sequencing is in [architecture.md](architecture.md#windows). Internals:

- `_probe_joined()`: live membership via the guest agent (`Win32_ComputerSystem.PartOfDomain`; `realm list` on Linux). Joins aren't idempotent (the single-config pass is strict), so resumes skip members already joined.
- `.nakon-domain-<team>-adds.json` existing means ADDS already ran (re-running `Install-ADDSForest` on a live DC fails). Promotion is skipped on resume; joins still run. `-ad-misconfigs.json` and `-ad-accounts.json` gate replays the same way.
- AD misconfigs run `strict=False`: they're scoring flavor, and "Disable System Firewall" sweeps every AD computer over WinRM, which a Linux realmd member lacks, so the step exits 1 on any mixed domain after landing.
- `deploy_domain_configs` aggregates `run_concurrent` results and fails the phase rather than discarding a chain that raised after promotion. Re-entering at phase 7 is safe (promotion probes + markers).
- Linux member join failures log and continue: the box keeps local auth and its services, and the realmd install can exceed nakon's per-step timeout on small VMs.

## common/timing.py

`timed(comp_dir, phase, op, target)` appends one JSONL line to `.deploy-timings.jsonl` (gitignored; op names and durations only; lock-serialized for concurrent workers). `print_timing_summary` totals per (phase, op) at deploy end.

## net/firewall_ops.py

A box with `"unmanaged": true, "in_path": true` is the team gateway (`utils.is_in_path_fw()`); `firewall_kind()` picks VyOS if the template name contains `vyos`, else pfSense.

- **pfSense config** (`generate_team_config`): string surgery on the comp's `pfsense/pfsense-config-orig.xml`. There is deliberately no pipeline-shipped seed; the surgery anchors on the factory config's exact stanzas and a fabricated seed fails silently mid-deploy. WAN `172.31.<id>.2/30`, LAN `192.168.<id>.1/24`, SSH on, outbound NAT off (the engine keeps MASQUERADE), WAN pass rule any→`lan`. The rule destination must be the `lan` keyword; a raw CIDR silently drops the rule. Compfile `firewall_dnat PORT->TARGET[,…]` adds LAN port-forwards (`{tid}` → team identifier). Seeds lacking it get the `<system><afterbootupshellcmd>` agent hook.
- **VyOS config** (`generate_vyos_commands`): fully generated. Pass A sets everything on `eth0` (WAN `172.31.<id>.2/30`, default route via the engine, SSH, DNAT); pass B (`generate_vyos_lan_commands`) flips `eth1` from the bootstrap `192.168.1.1` to the team gateway. Two passes are load-bearing: a commit moving eth1 kills the pushing SSH session, so B reconnects over the pass-A WAN. No filter rules: the 1.5/circinus revamp removed interface-attached rulesets and default-accept already matches pfSense's WAN rules; DNAT matches destination only. The script ends on a grep canary at the op-mode prompt plus `exit`; ending at `exit` leaves the canary on a fresh login prompt and wedges the PTY. Commits print nothing on success, so canary + clean exit are the only signals.
- **`_push_vyos`/`_pty_session`**: runs the CLI with no remote command (the login shell is the CLI) on a real 24x80 local PTY, expect-style, feeding the next line only after a prompt appears. Blind feeding wedges the CLI inside `save`, and `save` flushes the tty input queue, eating queued lines.
- **`bootstrap_firewalls`**: clones `pfsense-provision` or `vyos-provision` (vmids in [environment-facts.md](environment-facts.md#templates); built by `tools/build-{pfsense,vyos}-provision-template.py`). Contract: SSH on, deploy key authorized, LAN = `192.168.1.1/24`, serial console. VyOS: key in config, no `hw-id` pins ([usage-people.md](usage-people.md#vyos-firewall-template)). One team at a time (every clone boots as `192.168.1.1` on its isolated bridge and the engine holds only one borrowed route): the engine borrows `192.168.1.2/24` on that team's NIC (`ens19+`, sorted team-key order) and waits for SSH on `.1`. pfSense: replace `/cf/conf/config.xml` (`cmp` first; identical config is left alone, no reboot), detached reboot, wait for SSH on the WAN; a firewall already answering on its WAN is pushed there. VyOS: pass A over the bootstrap address, wait for WAN, pass B over WAN, no reboot. The borrowed address is released in a `finally`. With `node=` (phase 5 passes `ctx.node`) the pfSense agent layer logs the post-reboot agent ping as boot confirmation and appends `fw_agent_diagnostic()` readback to failures. Agentless clones are normal; VyOS has no agent layer.
- **`fw_agent_diagnostic`/`_agent_answers`**: `/agent/ping` rides virtio-serial, so it answers before guest networking and distinguishes "QEMU up" from "guest rebooted" (`qm status` uptime can't). Best-effort; absence must never become the failure.
- **Agent in the pfSense template**: pfSense never runs `/usr/local/etc/rc.d/*` at boot (`/etc/rc` globs `*.sh` only), so the package's rc script plus `qemu_guest_agent_enable` is inert. The only hook that fires is `<system><afterbootupshellcmd>` in config.xml (`/etc/rc.bootup` mwexec's it), so it must be in every config the firewall runs, including generated ones. Packages come from `pkg.freebsd.org/FreeBSD:14:amd64` matched to the guest's `pkg config abi`; `pkg add` needs `IGNORE_OSVERSION=yes` and a hand-staged dependency closure (the range has no DNS/internet, so any future pfSense package needs the same offline staging). The binary is `qemu-ga`; flags go in `/etc/rc.conf.local` with `-p /dev/vtcon/org.qemu.guest_agent.0`. `qm guest ping` doesn't exist on this PVE; use REST `POST /agent/ping`.
- **`cut_over_engine`/`cutover_netplan_yaml`**: rewrites `/etc/netplan/60-team-ifaces.yaml`: team NICs up with no address (`dhcp4: false` + `optional: true`; an empty entry defaults to dhcp4 and hangs), transit NICs `172.31.<id>.1/30` with the `192.168.<id>.0/24 via 172.31.<id>.2` routes, `netplan apply`, no reboot. NIC naming mirrors `team_nics`: team NICs `ens19+` in sorted team-key order, transit NICs continue the sequence.
- **`verify_in_path`**: fail-loud gate: WAN SSH, `ip route get` via `172.31.<id>.2`, first managed box reachable through the firewall. Remediation text is kind-aware (`pfctl -sr` vs `show firewall`).

## deploy.py + deploy_lib/

`deploy.py` is a shim (loads `.env`, re-exports `main`/`deploy`/`prepare`/`DeployContext`); `create-competition.py` loads `.env` before importing `deploy`, which reads `TF_VAR_*` at import (the second load is a no-op). Imports flow one way: `cli` → `runner` → `prepare` → step modules; `phases/` and `context` depend only on leaf helpers. Tests patch via `tests/_deploy_patch.dpatch(name)`, which replaces a name in every `deploy_lib` module that binds it.

| Module | Responsibility |
|---|---|
| `cli` | `build_parser`, competition selection, `write_new_competition`, `print_plan`, `main` |
| `runner` | `deploy()`: lock, `prepare()`, `run_pipeline` (walks `PHASES`, checkpoints, hooks `connect_terraform` after phase 2, honors a pause request at each boundary), `report_failure` |
| `pause` | `pause_at_boundary`: a run-queue hold (`TEZ_PAUSE_FILE`) parks the pipeline after the phase in progress, records `paused_after_phase`, and resumes when the request file goes |
| `prepare` | Ordered pre-phase steps filling one `DeployContext`; `None` if the operator declines |
| `context` | `DeployContext`, `save_state`, `checkpoint`, ownership-tag properties |
| `inputs`, `secrets`, `placement`, `configs`, `tfinputs` | Disk reads; run id/roster/secret mint and carry; placement + endpoint-keyed engine lock; stage configs, golden hashes, frozen gate; `terraform.tfvars.json`, `teams.json` |
| `golden_plan` | `golden_hash_entries`, `golden_rebuild_gate`: the per-box hashes and phase 4's rebuild decision |
| `coverage`, `failure` | Plant-coverage bookkeeping; `record_degradations` |

- **`prepare()`** order: resolve engine vmid; load Compfile/config/state; mint or reuse secrets; compute placement; generate nakon + stage configs; golden hashes + frozen gate; tfvars; preflight; enumerate targets. `gates.check_resume_gates` runs right after prior state is read and `gates.run_range_gates` (stale terraform state, then capacity/collision preflight) after placement, both **before the secrets mint**, so a refusal never leaves a credential-desync corpse. `.deploy.lock` is taken by `deploy()`; nothing in `prepare()` destroys anything.
- **State writes**: `save_state` → `config_ops.write_state` (atomic `os.replace`, 0600 at creation). `checkpoint(n)` sets `last_phase`, the resume guard's only source.
- **Pause at a boundary** (`TEZ_PAUSE_FILE`, set per run by tezcatlipoca-dashboard): the boundary is the only consistent place to stop a deploy — a phase can have a terraform apply or a VM build in flight, and cutting one open mid-call leaves terraform's view behind the estate (the mismatch the resume guards exist for). The hook runs at the end of each *executed* phase (a skipped phase is no boundary: its checkpoint never moved), writes `paused_after_phase` so an observer sees the hold without parsing stdout, and blocks until the request file is deleted — same process, same lock, resumed in place. `run_pipeline` clears a marker left by a run that died parked, so a resume cannot read as paused. Unset is a no-op, which is what keeps manual deploys unchanged.
- **Resume** (`from_phase > 1`): skips phases 1–2, reloads teams and secrets from state. Only *executed* phases checkpoint. `--from-phase N` with no state file is a hard `SystemExit` (else a fresh branch would mint new passwords and overwrite `teams.json` while skipping phases 1–2). Different `pipeline_version` refuses. `guard_resume_from_phase()` refuses `N > last_phase + 1`. A secret field missing from an older state is regenerated once and written back so a second resume reuses it.
- **Passwords** are generated per run (public repo; fixed literals are guessable). `box_password` is a golden-hash input (baked into `/etc/shadow` and cloud-init on the golden disk), so a fresh deploy carries it forward from prior state, and packet `passwords.json` outranks both; re-minting would rebuild every golden.
- **Bundles** are built operator-side (the only vulndb-dependent steps): three stage bundles, so the engine never sees vulndb credentials and no full-machine pass is deployed.
- `all_targets` is built once from the full box list; nothing may re-derive a vmid from a filtered list. `teams.json` is persisted because destroy and verify read it.
- A fresh deploy unlinks `.postclone-swept`; apply #2 unlinks it again after re-creating team boxes so a phase-6 resume always re-sweeps fresh clones. `confirm_deploy()` is skipped on resume and by `--yes`.
- **Phase 1**: reclaims the untagged interrupted clones (ownership `MARKER`) at this deploy's slots, then resets the comp dir — `reset_domain_markers()` clears `.nakon-domain-*` so a stale marker can't skip a promotion, `.postclone-swept` and the previous teardown's journal go too. No VM a run owns is touched: preflight refuses a leftover range and `destroy-competition.py` reclaims it.
- **Phase 2**: `-parallelism=1` (concurrent full clones saturate the datastore/API, HTTP 596). Engine SSH is polled (`wait_for_ssh`) and `forget_engine_host_key` drops the stale pin. Records `deployed_endpoint`.
- **Phase 3**: `event.conf` before any nakon run prevents the Quotient crash loop that wipes NAT.
- **Phase 4**: after the golden set and apply #2: Windows bootstrap for all teams, concurrent waits, `setup_ubuntu_auth`, DNS fix, apt prep, `tz-base`. Clones boot with working DNS/apt because the golden disk carries no disruptive configs.
- **Phase 5**: print-and-return without an `in_path` box (keeps phase indexes stable). Order is load-bearing: the engine owns bridge addressing while configs are pushed, and phases 6–7 mustn't run before cutover. Records `state["firewalls_bootstrapped"]`; resume re-pushes (unchanged config isn't reapplied). Firewall `tz-base` is taken after cutover.
- **Phase 6**: `.nakon-predomain.json`, lenient (`strict=False`: one flaky plant mustn't kill a sweep that's 98% landed), `--jobs N`, gated by `.postclone-swept`. `fix_services_on_boxes` runs after it (the ssh-\* configs restart sshd and can trip start-limit).
- **Phase 7**: domains run concurrently per team; then the final pass; team beacons (`team_beacons 1`) are planted before `tz-ready` so restore points carry them. **Assume-breach** (`assume_breach 1`, `red/red_plant_ops.py`) deploys bad-auto's red01 and the realm engine DNAT and runs the day-0 seed (access, prebaked Realm C2 beacons, persistence/evasion), also before the snapshot, so boxes are compromised at T0. With `realm_c2_local 1` (default) the C2 lives on red01: `red/badauto_config_ops.py`/`red/realm_c2_ops.py` write the comp's bad-auto config before the deploy (reserved red01 vmid/IP + realm layer; the engine DNAT bakes in `realm.c2_ip`), then build tavern from source on red01 with every transport (`grpc` + http1/dns/quic/icmp redirectors) and the MCP server (`ENABLE_AI_MCP=1`, `/mcp`), relaying prebuilt imix implants from `realm_c2_implant_host` (`realm_c2_repo`, `realm_c2_go_version` override the upstream URL/toolchain). Provisioning failure degrades rather than aborts; `realm_c2_local 0` keeps the C2 on VM101. Red01's identity goes to `state["assume_breach"]`; teardown runs `badauto destroy` for it (run tags never covered it) under the same refusal contract (`TEARDOWN-REFUSAL {saw,expected,remedy}`, exit 17), and bad-auto pulls red's evidence before its VM delete. `nakon_failed_steps` is merged across passes (deduped, capped at 40).
- **Phase 8**: Quotient is polled (`/api/login`). Sub-steps are flag-gated because `unpause_engine` isn't idempotent; in the crash window between POST and flag-save it asks the engine first (`engine_paused`) and re-POSTs only if still paused.
- **Failure handler**: names the resume phase. An "already exists" error at phase ≥2 suggests Proxmox/terraform state mismatch: a leftover range is a preflight refusal now, so reclaim it with `destroy-competition.py` and deploy fresh (a `--from-phase 1` re-run refuses the same leftovers), or resume the run that owns it.
- `credentials.txt` (0600) is the authoritative record of generated secrets (incl. `box-login`, `box-credlist-*`). `--plan-only` exists because nothing otherwise confirms between the box picker and a real deploy. `--competition` skips straight to `deploy()` but boxes are still interactive.

## pipeline_api.py

The import surface for the hyphenated entry points. Explicit `__all__`, each name imported from its owning module, no re-export chains; add a name only when a consumer needs it, so the consumer set stays visible.

## Teardown (`teardown/`)

One engine: `infra.ownership.Ownership` (destruction identity), `Refusal` (saw/expected/remedy, with the `TEARDOWN-REFUSAL` line bad-auto also speaks), `Inventory` (classified estate view, fail-closed on listing errors), steps with declared failure policies (`REFUSE` aborts; `WARN_AND_PROCEED` records), `TeardownPlan` (evidence-before-destruction validated at construction), `TeardownEngine` (gates → completion journal → steps → `verify_clean`, the only exit-0 path). Mechanics are in `teardown/machinery.py` (pre-stop, bounded destroy with recovery, tagged sweep, template teardown, gates). `destroy-competition.py` and the scrim harness (via `teardown/runner.run_terminal_teardown`) are shims. RECLAIM is the plan kind a reclaim path composes; deploy phase 1 no longer builds one (a deploy destroys nothing a run owns) and uses `ownership_verdict` directly for its interrupted-clone reclaim. Tests patch `teardown.machinery`, `artifacts_ops`/`red_plant_ops`, `infra.ownership`, `teardown.runner`.

Exit 0 = `verify_clean`: plan completed, nothing of ours running or tagged (teams-only reports kept templates as kept-by-design), red01 gone, no state lock, evidence archived, no node unlistable. Order: evidence → portal → red01 → pre-stop → `terraform destroy` → templates → remote access. The template step is `full`-gated: `destroy-competition.py --teams-only` and the harness's `run-agent-scrim.py --keep-templates` both reach it with `full=False` (the harness keeps `keep_templates` in the run manifest, OR-semantics, so a resume cannot drop the intent the first run had).

- **TF_VAR restoration**: `TF_VAR_teams`/`boxes_per_team`/`event_name` come from saved files so `for_each` keys match the original apply, plus the comp's endpoint/node/vmid, mirroring the deploy's stale-state guard (a destroy against another host makes terraform reconcile foreign resources).
- **Ownership**: `run_id` anchors everything; `main()` refuses without it. Pre-stop stops only name-matching VMs that also carry the ownership tags; `sweep_tagged_leftovers` needs the full set; golden/engine/jump destroys pass the role-narrowed set.
- **Golden wave**: after `terraform destroy`, `destroy_golden_set` API-destroys templates (`engine_vmid + 150 + i`) with the ownership check.
- `terraform destroy -parallelism=4`: deletes are metadata-light.
- `load_destroyable_competitions()` requires `Compfile` + `teams.json` + `boxes.json`.
- **Hung Windows DC**: bpg shuts down gracefully, and a DC with a dead agent never does, so `terraform destroy` hangs. Hard-kill the qemu process (`kill -9 $(cat /var/run/qemu-server/<vmid>.pid)`) or `qm stop` the other DCs first.

## redeploy

CLI + dispatch in `redeploy-competition.py`; `redeploy_gate_ops` (pipeline_version/run_id gate before any Proxmox call), `_select_ops`, `_plant_ops`, `_light_ops` (resync/rollback/reconfigure), `_rebuild_ops`, `_reset_ops` (ladder + probe), `_engine_ops` (`engine_recovery`, `reseed_event`). Modes: [usage-agents.md](usage-agents.md#redeploy-modes).

- Pre-domain re-plants (`rollback-base`/`reconfigure`/`rebuild`) run the post-clone stage (`.nakon-postclone.json`), not the full config; golden installs ride the linked clone.
- `rebuild` clones `state["golden_template_ids"]` and refuses if the golden is gone; it never clones the anchor team's live box.
- On a domain-role box, `rollback-base`/`rebuild` rerun `deploy_domain_configs()` after deleting the stale ADDS done-marker. If the chain can't run, `tz-ready` is not retaken. `reconfigure` assumes membership is intact.
- `box_platform()` goes through `pipeline_api.os_to_platform`.
- `fix_dns_on_boxes`/`setup_ubuntu_auth`/`fix_services_on_boxes` get Linux targets only on every path.
- Rollback modes fail fast (with `snapshot_support_hint`) when a selected box lacks the snapshot, before touching anything.

## verify-competition.py

The entrypoint calls `verifier.cli.run()` (Ctrl-C → 130) and re-exports the gate API under `__all__` for tests that load it by path. Gates call each other's hops through the defining module (`context.ssh_via_gateway`, `scoreboard.check_services`), so a test patches one place.

| Module | Responsibility |
|---|---|
| `cli`, `runner`, `model` | Flags and main; `run_gates()` ordered sequence → `RunOutcome`; `Status`, `GateResult`, `CheckError`, constructors `gate_pass/fail/skip`, `bool_gate` |
| `verdict`, `summary` | Exit code + freeze dict; printed SUMMARY (both derived from the same results) |
| `context`, `loaders`, `boxes`, `creds` | Terraform `agent_context`, ssh helpers, `REPO_ROOT`; comp-dir readers; machine predicates; admin-password lookup |
| `scoreboard`, `engine`, `isolation`, `red`, `misconfig`, `domains`, `state_gates`, `freeze`, `packet`, `firewall` | One gate concern each |

- **Tri-state**: `SKIP_UNAVAILABLE` is non-passing when the result gates. A `gating=False` result prints `[informational]` and is excluded from the verdict. `GateResult` carries its SUMMARY `label` so printed word and verdict can't disagree. `--allow-unverified <gate>` waives only a SKIP; unmatched names warn.
- TLS warnings are silenced (self-signed TLS elsewhere would drown output).
- Boxes authenticate with the Proxmox key; the admin password is parsed from `credentials.txt` (override `--admin-password`) falling back to `changeme123`.
- `MISCONFIG_CHECKS` documents how to verify each mapped config: `suid-find` (`s` in the owner-exec slot of `ls -l $(which find)`), `www-data-shell` (`/etc/passwd` line ends `/bin/bash`), `bad-perms-userConfig` (`/etc/shadow` mode 666), `writable-sudoers` (`/etc/sudoers.d` mode 777). The spot-check picks a box/config pair it can verify.
- `check_services()` returns a **list** of `GateResult`s (service gate + `pins_registered` when pins exist) on every path. No scored rounds yet is "not yet scored", not a failure. `Result` values are normalized (`"false"`/`"0"` fail) since Quotient returns strings. Under `--strict-services` nothing-scored is a FAIL. Pins always gate.
- `check_isolation()`: the DROP match requires both `-s` and `-d` (`192.168.0.0/16` appears ≥2×). With ≥2 teams it also runs a cross-team connection (expected blocked) plus an internet-reachability control and a target-liveness probe. A dead target and a blocked one look identical, so an unprovable probe is SKIP.
- `check_misconfig()` survival: groups keyed on normalized config **names** (dict entries are unhashable, and "same box" means same names regardless of `vars`). Present on some teams only FAILs; absent on every team FAILs; all probes unprovable is SKIP.
- `check_domains()`: per team, `Get-ADDomain` answers for `team<id>.local` with a syntactically valid `S-1-5-21-*` DomainSID (with one team, uniqueness is vacuous, so shape is what gates), `svc-support` exists, every member joined. Duplicate DomainSIDs FAIL; duplicate member machine SIDs are INFO (linked clones; isolated forests). One team PASSes with "uniqueness needs a second team". Malformed `domain_roles.json` FAILs; only absence skips.
- `check_plant_coverage()` fails closed (see [usage-agents.md](usage-agents.md#verify-competitionpy)). `record_coverage` clears a machine's stale failures on a clean replant. The golden plant records `{box}-golden` / `{box}-golden-slot{N}` (slot configs name machines identically, so keys mustn't collide) on every completion path; a skip guarantees the record exists without erasing a prior failure; an `alpine_services`-tolerated failure is recorded since it's a real golden gap. A failure on any slot's golden flags every team copy.
- `check_round_loop(fix=…)` is consumed by main and FAILs for the current run; `--fix-round-loop` POSTs start/unpause, then rerun verify.
- Exit code: isolation and misconfig are never optional (a failed isolation check means teams can reach each other now; a misconfig missing on one clone breaks the same-misconfigs fairness guarantee). Down services are soft unless `--strict-services`.
- Remote loops (`check_domains`, misconfig probes) use the 8-worker pool (pure SSH/agent probes) and re-serialize aggregation, so output and exit codes are unchanged (`tests/test_parallel_verify.py`).

## generate-packet.py

Local-files → Markdown. The packet is the same for every team (real credentials are issued at start via `credentials.txt`) and never reads `box_vulns.json`. `_SERVICE_DISPLAY` mirrors `quotient/setup.py`'s Display fields as a smaller separate table (no dependency on Quotient's TOML shapes). `load_inject_schedule()` emits titles and offsets only.

## packet_ops / compile-packet / run-schedule

- `validate_profile()` surfaces everything that would die mid-deploy at compile time (comp-name/unix-username validity, legacy-account collisions, domain `name_template`, credlist/domain cross-requirements, box count/name/octet/template/fidelity/role rules, exactly one DC, pin and display uniqueness, inject offsets). Template validity is only "non-empty"; resolution is the preflight's job.
- Emits `Compfile` (with `domain_prefix`/`domain_suffix`/`packet_source`), `boxes.json`, `users.json`, `box_services.json`, `domain_roles.json`, `injects/`, `packet-fidelity.md`, and 0600 `passwords.json`, `domain_accounts.json`, `box_baseline.json`.
- **Score-only pins** (`score/tcp`) score a native service the catalog can't plant; they're stripped from the nakon machine list and catalog check but present in `event.conf` and `expected_service_names`.
- `run-schedule.py`: the engine has no schedule model; pausing is the schedule. With `--execute` it drives `start`/`freeze`/`resume`/`end` from the packet's `schedule:`; `end` captures the scoreboard and injects into `<comp>/evidence/` before pausing.

## Multi-node and sync

[multi-node.md](multi-node.md) is the full treatment.

- Placement is capacity-fill. Per-host tokens are referenced by env-var name (`token_env`). `activate_placement()` swaps `TF_VAR_proxmox_*` to the engine host before node-scoped work and restores afterward. An existing `placement.json` is authoritative; a resume without one adopts its deployed endpoint. `golden_vmid_for_slot()` shifts the golden block one box-stride per satellite (`engine_vmid + 150 + slot*10 + box_idx`) because linked clones can't cross hosts; `jump_vmid_for()` is `engine_vmid + 130 + slot`.
- `net/jump_ops.py`: per-satellite Alpine VM impersonating the engine at L3 on the satellite's bridges (holds `192.168.<id>.1`), DNATing the gateway IP's apt-cacher port to the engine and SNATing engine→box traffic so boxes' gateway-only SSH trust holds. Forwarding is default-DROP with explicit accepts. NIC hotplug past the first is unreliable: reboot instead.
- `routing_ops.verify_satellite_routing()` is the fail-loud gate after apply #1. The routes come from `team_nics` (`placement["satellite_routes"]`, plus a oneshot unit so an engine reboot re-asserts them); this module only proves them.
- `template_sync_ops`/`sync-template.py`: `vzdump --stdout | ssh qmrestore` over root SSH. Satellites need every box template plus an Alpine base for the jump VM.

## quotient/setup.py

- `unpause_engine()` is separate from `seed_teams()` because unpausing isn't repeatable (hence the `engine_unpaused` flag); seeding is idempotent. `seed_teams` looks IDs up by name, batch-updates identifiers + `active`, sets `started`. `create_injects` is a multipart POST per inject, skipping existing titles; if existing injects can't be fetched it proceeds without dedup and says so.
- Auth is cookie-based (`POST /api/login`); no bearer tokens.
- `build_event_conf()`: IPs are `192.168._.<last_octet>`. `StartPaused = true`; `Delay 60`/`Jitter 10`/`Points 5`. The `inject` account is emitted whenever an inject password is supplied (INJECTAUTH routes accept `admin` and `inject`). Check identity is `<box>-<Display>`; duplicates are a config-load error, so same-type pins need distinct Displays (a collision is a generate-time `SystemExit`, never a silent drop). Dict pins merge `PIN_CHECK_OVERRIDES` over the base config. `expected_service_names()` shares pin resolution with verify. `CredlistSettings` is emitted only when a check needs it.
- `_SERVICE_TO_CHECK` keys and fields must match Quotient's Go struct TOML tags (the check-type key on `Box` is case-sensitive; fields are case-insensitive). A box accumulates checks (apache + bind → Web + Dns); vulns and unknown names are skipped with a warning. By family:
  - **Web**: `Url` is a required array of `{Path, Status}`. **Dns**: `Record` is a required array of `{Kind, Domain, Answer}`.
  - **Ssh**, **Smtp**, **Imap**: `CredLists` required (`smtp.go` always calls `getCreds`; Imap's triggers the mailbox-list check).
  - **Ftp**: authenticates against `linux.credlist`; `unauthorized-ftp-server` installs vsftpd with `anonymous_enable=NO`/`local_enable=YES`, so only a local login passes. Windows FTPS answers `534 Policy requires SSL`, so the CDE profile plants it (`plant_only`) and scores :21 with a Tcp check.
  - **Sql**: `Kind` must be explicit (`mysql`); authenticates against the DB user table, which `fix_services_on_boxes()` populates from the credlist. Don't drop `CredLists`.
  - **Telnet**: generic `Box.Tcp` (dial and UP on connect) is enough.
  - **Windows** (`Enable WinRM`, `New SMB Share`, `RDP misconfigs`): Quotient has no SMB/RDP/WinRM check (only Web/Dns/Ssh/Ftp/Smtp/Imap/Sql/Tcp), so these use Tcp port-open; `IIS HTTP` is a real Web check (`/` → 200). Keyed by exact catalog config name.
  - **ADDS**: scored (Tcp :389), never planted from `box_services.json`.

## terraform/

**main.tf**

- Providers: API token, `insecure = true`, **no `ssh` block** — bpg needs SSH only for file/snippet uploads, `source_file.path` imports and container `idmap`, and this config has none of those (VM, network and storage management are API-only).
- `team_bridge` (+ `_sat1..4`): **no `ports`**, so an empty bridge has no uplink. Slot 0 is the engine node; `_satN` carry the satellite alias. Exactly `MAX_SATELLITES=4` aliases exist; unused slots carry dummy settings.
- `transit_bridge`: `vmbrW<id>` per slot-0 team only when a box is `in_path` (`local.has_in_path_fw`); otherwise `for_each` is empty and plans match the no-firewall pipeline. Engine transit NICs append after team NICs so netplan names never shift.
- `scoring_engine`: `vm_id = var.scoring_vm_id`; `agent { enabled = true }` to read the real mgmt IP. No cloud-init block: the template's netplan and baked key/sudo suffice. The mgmt IP is static by default (`TF_VAR_engine_mgmt_ip=''` restores DHCP); `engine_template_ops` uses it for the build VM's `ipconfig0`.
- `team_box` + `team_box_sat1..4`: five slot-dimensioned resources, `for_each = var.build_team_boxes ? local.all_team_vms_by_slot[n] : {}`. Every team is Terraform-managed. `clone { full = false }`, no `retries` (templates aren't locked like clone-source VMs).
- `local.all_team_vms_by_slot`/`all_team_vms`: per-slot team→box maps with `ip`/`gw`/`bridge` from `team.identifier` + `box.last_octet`, matching `enumerate_targets()`. Keys: `team1-<box>`, `<identifier>-<box>`.
- `lifecycle` precondition: the computed vmid is checked at plan time against `scoring_vm_id`; asserts one `golden_template_ids` entry per box when building; at most one box is `in_path`.
- `template_ids`: resolved from a tag-filtered data source **excluding running VMs** (a running box with a stray `template` tag duplicated names and failed every apply).
- `dynamic "disk"` only when `disk_gb` is set (null makes bpg default to 8 GB, triggering an unsupported shrink). Disk interface uses `coalesce`, not `try` (`try(null, "scsi0")` returns null).
- `dynamic "initialization"` only for non-Windows and non-unmanaged (Windows has no cloud-init agent; appliances self-configure via `firewall_ops`).
- `user_account.password = var.box_password`: nakon's paramiko uses password auth.
- `dns.servers` is always a public resolver, never the `dns*` box: pointing boxes at it deadlocks provisioning (its bind9 is installed by nakon via apt, which needs working DNS). `fix_dns_on_boxes()` forces 8.8.8.8 anyway, so the `dns*` box never serves its team; to change that, repoint boxes after nakon.
- `scoring_mgmt_ips` excludes loopback, `192.168.`, Docker's `172.16/12`, and Tailscale `100.64/10`; first survivor wins.
- `null_resource.team_nics`: triggers carry identifiers only (team passwords would print in every plan). NICs: ens18 mgmt, ens19 team1, ens20 team2, …, transit NICs after. `chmod 600` (netplan refuses world-readable); sysctl `ip_forward=1`, `rp_filter=2`. Depends on the reboot resource. Multi-node: also writes satellite routes.
- `null_resource.reboot_scoring_engine`: a **cold boot** is required (guest reboot doesn't trigger the PCI scan that detects new VirtIO NICs); hard-stop (`shutdown=0`) and start via API.
- `null_resource.orchestrate`: SSH readiness probe against the engine, 30×10 s plus 30 s settle; after the reboot and `team_nics`.
- `team1_key = "team1"`: anchor looked up by name, not sort order.

**variables.tf**: `vm_username` is the OS account already on every template. `box_username` is themeable via `users.json`. `box_password` is generated or carried (nakon uses password auth). `teams` defaults are placeholders kept at 101/102 so a hand-run `apply` matches the engine's rules. `boxes_per_team.disk_gb` omitted keeps the template's size **and** its storage pool (`var.datastore` only applies when the disk block is emitted). `disk_iface` defaults `scsi0`; use `sata0` for Windows templates that boot from SATA. `template` must match a tagged VM name exactly. Others: [usage-people.md](usage-people.md#configure-the-event).

## Conventions

- nakon is a CLI dependency only (`randomize`/`build`/`deploy`); no import, no direct MySQL.
- `vendor/nakon` is pinned to a release tag; bump deliberately; develop nakon in its own repo.
- `vendor/nakon/.env` holds vulndb creds (build time only). Bundles live under `vendor/nakon/bundles/`, content-addressed and shared across competitions.
- Per-run secrets are gitignored; `boxes.json`, `Compfile`, `box_services.json` are tracked.
- Pinned `box_services.json`/`box_vulns.json` make resumes replay the same bundle instead of re-randomizing.

## artifacts_lib

`artifacts_lib/artifacts_ops.py` is a facade re-exporting the package's public names (plus `shutil`/`subprocess`, which tests patch through it). `lifecycle.collect_for_teardown` resolves its transport via `artifacts_ops.default_transport` at call time, so patching `destroy.artifacts_ops.default_transport` steers teardown; elsewhere patch the owning module.

| Module | Owns |
|---|---|
| `constants`, `env`, `paths`, `hashing` | File names, status vocabulary (`OK`..`SKIPPED`, `LOST`), `SIDES`, `Unreachable`; `iso`, `git_facts`, `in_worktree`; `artifacts_root`, `test_dir`, `test_key`, `run_id_from_state`; `sha256_file`, `seal_local_file`, `write_bytes_atomic` |
| `manifest`, `plan` | test.json (`ensure_test`, `record_phase/paths`, `update_manifest`); `plan_targets` (pure), `want_label` |
| `transport`, `collect` | scp / guest-agent / local / ssh fetchers; `collect`, `load_collection`, canonical-document derivation |
| `verdict`, `status`, `report`, `index` | `ingest_verdict` (parses INTERACTION.md); `document_state`, `why_missing`; report skeleton; `update_index`, `list_tests` |
| `archive`, `seal`, `lifecycle`, `cli_*` | `archive_test`; `verify_test`, `seal_test`; `finalize`, `collect_for_teardown`; `test-artifacts.py` subcommands |
