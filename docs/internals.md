# Internals — design notes

The "why" behind symbols, constants, orderings, and workarounds. System-level material (phases, data flow, isolation, state schemas, nakon contract) is in [architecture.md](architecture.md) and not repeated. The scrim harness is in [scrim-harness.md](scrim-harness.md). Re-verify timeouts and symbol names against the tree before relying on them.

## common/constants.py

Single source for vmid math, snapshot names, nakon budgets, naming.

| Symbol | Value | Why |
|---|---|---|
| `SCORING_ENGINE_VMID` | 1000 | Default only; `--scoring-vmid` / `TEZ_SCORING_VMID` overrides. 100 collides with an unrelated VM on the primary node |
| `DEFAULT_ENGINE_MGMT_IP` / `_GW` | `10.0.0.250` / `10.0.0.1` | Static: a DHCP engine can reboot onto a new address while the recorded one goes stale (shakedown-5x4: .221→.243→.233) |
| `MAX_BOXES_PER_TEAM`, `MAX_TEAMS` | 10, 154 | Stride-10 vmid block per team; identifiers 101–254 |
| `SNAP_BASE` / `SNAP_READY` | `tz-base` / `tz-ready` | |
| `PER_MACHINE_NAKON_BUDGET` | 2400 s | `run_nakon` timeout is `max(2400, budget × machines)` |
| `SLOW_SERVICES` | `splunk`, `roundcube` | Excluded from auto-randomize |
| `DISRUPTIVE_CONFIGS` | `resolv-conf-null-dns`, `apt-sources-empty`, `apt-hold-all-packages`, `dpkg-broken-hold-state` | Break DNS/apt; sort last in a pass |
| `DOMAIN_INFRA_CONFIGS` | `ADDS`, `Domain Join`, `domain-join` | Scored but never planted from `box_services.json`: `domain_ops` injects them per team with domain/DSRM vars a golden plant can't carry |
| `PIN_CHECK_OVERRIDES` | `display`, `port`, `path`, `scheme`, `status`, `credlist` | Per-pin scoring overrides for dict pins. Quotient needs unique `<box>-<Display>`, so `display` lets same-type pins coexist. Stripped from nakon machine lists |
| `PREDOMAIN_STAGE_CONFIGS` / `FINAL_STAGE_CONFIGS` / `ACCOUNT_FILE_MODE_CONFIGS` / `POST_CLONE_CONFIGS` | sets | Stage membership ([architecture.md](architecture.md#data-flow)) |
| `REQUIRED_VARS` | map | Curated (the catalog has no required-vars metadata). `"ip"` = machine identity, auto-filled, banned from the golden stage; `"ip:<box>"` = same team's copy of another box; `"literal"` = operator pins `{"name","vars"}` |
| `GOLDEN_VMID_OFFSET` / `GOLDEN_IP_BASE` / `GOLDEN_TAG` | 150 / 240 / `tezcatlipoca-golden` | Goldens sit above the engine slot; IPs `.240+` on team1's subnet are free because goldens convert before team boxes exist |
| `ENGINE_TEMPLATE_VMID_OFFSET` / `ENGINE_TEMPLATE_NAME` | 140 / `engine-template` | Just below the golden block so one preflight scan covers both |
| `GOLDEN_CLONE_TIMEOUT` | 5400 s | A 60 GB Windows full clone took >30 min on HDD-ZFS |
| `WINDOWS_ADMIN_USER` | `Administrator` | |
| `NAKON_DIR` | `vendor/nakon` | cwd for nakon subprocesses so they read its `.env` |

## common/utils.py, ssh_ops.py

- `DNS_FIX_CMD`: cloud-init ignores `dns.servers` on static IPs, so DNS is repaired live (`/etc/resolv.conf`, `.head`, a `resolved.conf.d/upstream.conf` drop-in, restart `systemd-resolved`; 8.8.8.8). `DNS_FIX_CMD_ROOT` is the same without `sudo`, for the guest-agent path (needs neither sudoers nor network).
- `load_compfile()`: key=value; non-numeric `difficulty` → 0. `compfile_flag()`: integer knobs (`nakon_jobs 4`), default when absent.
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
| `common/targets.py` | `vm_id_for`, `enumerate_targets`, and the frozen roster (`persist_targets`/`frozen_roster`/`load_targets`) in the document's `roster` section |

- **Ownership** (`ownership_verdict(node, vmid, raw_tags, expect_tags)`): `owned` (full set), `marker` (no tags + this competition's clone marker), `unproven` (no tags, no marker), `missing` (tags present, set incomplete). `destroy_vm_if_exists` (refuses unless owned/marker) and the preflight collision gate (ours = owned/marker) share it, so teardown and preflight can't disagree.
- **Run ids**: `utils.mint_run_id` mints `run-<8 hex>` once per competition dir (persisted in the document, reused by resume/redeploy). `estate.apply` stamps the full ownership set on every VM it creates; the golden/jump/engine-template creators build it themselves, and an adopted template is re-stamped for the current run. The comp tag is shared by every worktree running that competition ID, so destruction identity is the full set. Preflight treats a comp-tagged VM without the run tag as a collision.
- The API runs with `verify=False` by default unless `proxmox.tls_fingerprint` (cert pinning) or `proxmox.ca_bundle` is set; entry points call `urllib3.disable_warnings()`. `proxmox_api()` retries `ConnectionError`/`Timeout` up to 4× (never HTTP errors). Task wait is 1800 s.
- `guest_agent_exec_root`: bash as root over virtio-serial; bypasses broken sudo, needs no network; returns `(rc, out, err)`, raises on agent failure. `guest_agent_exec_windows`: PowerShell as SYSTEM via `-EncodedCommand` (UTF-16LE base64); the only channel into Windows before bootstrap. `wait_for_guest_agent` returns bool.
- `stop_vm`/`start_vm`: ACPI first, forced stop only if ignored (for VMs that survive; teardown force-stops). `take_snapshot` is disk-only, replaces a same-named snapshot, and **never raises** (optional recovery; thick LVM can't snapshot). `rollback_snapshot` needs stop/start and raises. `list_snapshots` returns an empty set when the VM is gone or the API refuses, so callers fall back to a mode needing none.
- Phase-1 cleanup: scans only the vmids this deploy plans to use (deliberately broader than the recorded inventory: a skipped clone would let a resume land on a stale disk with old passwords) and destroys only VMs whose `ownership_verdict` is MARKER. Every destroy needs the PRIOR run's ownership set (`ctx.reclaim_tags`).

### Preflight (`infra/gates.py`)

`run_preflight(plan)` is the one gate before Proxmox mutation. A `PreflightPlan` is a list of `NodeShare`s (single-node = one share over the cluster view; multi-node = one per hosting node). Order: remote-access gate (if enabled); per share, template checks, vmid and bridge collisions (via `ownership_verdict`), datastore headroom; then engine and jump mgmt-IP gates, the red01 gate (`infra/red01.py`), and the nakon catalog check. Concurrent deploys aren't warned about: per-resource gates refuse real collisions and `acquire_engine_lock` (`nakonops/nakon_lock_ops.py`) serializes deploys sharing one engine. Call `run_preflight` before any new mutation path.

## config/config_ops.py

Team identity/secrets and persistence; prompts in `config_prompts.py`, injects in `injects_config.py`.

- `list_proxmox_templates()` resolves the base templates by the `template` tag alone (`config/config_prompts.py`) and excludes the engine's own template VM.
- `collect_teams()`: `identifier = 100 + i`; refuses identifiers whose 10-slot vmid block overlaps the engine, engine-template, or golden block.
- `collect_boxes()`: per competition; reuse replays `boxes.json`. Blank `disk_gb` keeps the template's disk (`estate.apply.ensure_disk_size` grows only when set; Proxmox can't shrink).
- `collect_users_config()`: credlist input must be exactly 3 names, else defaults.
- `resolve_inject_times()`: offsets are minutes from competition start, resolved to RFC3339 in phase 8 so they anchor to the real start.
- **Inject timing**: `rollback-ready` does not reopen injects (offsets stay anchored). `--reset-event` re-clones the engine and reruns the seed, re-anchoring at now; `verify` WARNs on injects already past close. The scrim path calls `scrim.inject_sync.reanchor_injects()` right after the event clock starts, via UpdateInject (`POST /api/injects/{id}`), re-listing every `InjectFileNames` entry because UpdateInject deletes unlisted attachments.

## nakonops/

Nakon is a subprocess with `cwd = NAKON_DIR`. Patch a name on the module whose function uses it.

- `os_to_platform()`: `"windows"` iff `"win"` in the template name; `redeploy`'s `box_platform()` goes through it.
- `_nakon_randomize()`: on failure names the real prerequisites (vulndb reachable, or `VULNDB_UI_URL`; check `vendor/nakon/.env`).
- `generate_nakon_config()`: if either pinned file exists it is honored, and **both files are always (re)written** so later reads don't fail on a half-pinned set. Otherwise one randomization per box type (identical services per team, which Quotient's wildcard IPs require). Budgets: `ceil(difficulty/3)` services (min 1), `difficulty` vulns (min 1). Disruptive vulns sort last (entries may be strings or `{"name","vars"}`). A second pass fills `REQUIRED_VARS` identity vars.
- `generate_stage_configs()`: `.nakon-golden.json` (one machine per box type at `192.168.<team1 id>.<240+i>`, minus the post-clone subset), `.nakon-predomain.json`, `.nakon-final.json`, `.nakon-late.json` (account-file mode pins, by name or by a var naming one of those files), and `.nakon-postclone.json` (union, for redeploy). `unbooted` types (DCs) get no golden machine; their plants move into the pre-domain stage. Empty subsets are dropped. All carry `box_password`. Multi-node adds `.nakon-golden-slot<N>.json`.
- `build_nakon_bundle()`: content-addressed; same error contract as randomize.
- `run_nakon()`: scp the nakon package, bundle, config to `/tmp/nakon-<tag>`, stage to `/opt/nakon/<tag>/`. Staging is per run, so concurrent runs don't race: each removes only its own dirs, the timeout `pkill -9 -f '/opt/nakon/<tag>'` is scoped to the run, and paramiko is **baked into the engine template** (`python3-paramiko`, installed with the other engine packages) so the deploy only probes for it — a pre-bake engine is refused by name, never pip-installed into. `only=` scopes machines without changing bundle content. `strict=True` passes `--strict`. `jobs>1` passes `--jobs` (Compfile `nakon_jobs`, default 4); the ceiling is engine egress/CPU and mirror throughput, not the datastore. Redeploy keeps `jobs=1` to stay gentle on live boxes.
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

- `wait_boxes_settled()`: gates on cloud-init first — `wait_boxes_cloud_init()` polls `cloud-init status` up to 600 s, so a freshly cloned box whose boot-time `package_upgrade` would bounce sshd mid-build is finished before the shared apt/build session opens; `error` warns and proceeds, still-running at the bound fails the step. It then polls the apt condition (agent ping, no `unattended-upgr` process, dpkg lock free via `apt-get check` with a 1 s lock timeout) instead of sleeping out the post-boot upgrade storm; ceiling 240 s, then falls through to prep's retry ladder. Once per `prep_apt_on_boxes` call and once in the engine-template build.
- `prep_apt_on_boxes()`: 5 attempts, 15 s apart; `command -v apt-get || exit 0` makes Fedora/Alpine an honest no-op.
- `fix_dns_on_boxes()`: 8 attempts, 15 s apart, then guest-agent root with `DNS_FIX_CMD_ROOT`. All-fail aborts.
- `setup_ubuntu_auth()`: sshd password auth + NOPASSWD sudo for `box_username`; 8 attempts, then guest-agent root.
- `fix_services_on_boxes()` (`box_creds` required, no fallback; script base64-encoded). Always:
  - Creates credlist OS accounts — a `useradd` for each, which recreates `/etc/passwd`, `/etc/shadow` and `/etc/group` at their default modes. Account-file mode pins therefore plant in the deploy's last pass, after this and after the assume-breach seed (`constants.ACCOUNT_FILE_MODE_CONFIGS`).
  - **Un-wedges sshd**: several catalog configs each `systemctl restart ssh`, and back-to-back restarts trip systemd's start-limit and leave sshd down for the event. `reset-failed` + conditional `start`, over SSH if up, else the guest agent. This is why it runs **after** the pre-domain pass.
  - Per service: MySQL/MariaDB bound to `0.0.0.0` + credlist DB users (`@'%'`, `GRANT ALL`, first also `WITH GRANT OPTION`); Postfix `inet_interfaces = all` and the `smtp/inet` master.cf entry; vsftpd anonymous off, local+write on; nginx restart; Dovecot plaintext auth (the `auth_allow_cleartext` key only on ≥2.4, which renamed it) plus a mail dir per account; bind9 `allow-query { any; }` + forwarders, recreating `named.conf.default-zones`/`db.local` if stripped; telnet via `update-inetd --enable`; Splunk: nakon only creates a user, so lighttpd on 8000 gives Quotient's check a listener; finally start every installed service.
  - If the SSH run exits non-zero (writable-sudoers-type misconfigs break sudo), retry the same script via guest agent as root with `sudo ` stripped by `re.sub(r"\bsudo ", …)`; `\b`, not `^\s*`, so mid-pipeline uses are caught.

## golden/

`golden_target_ops` (slot arithmetic, `golden_targets`, `unbooted_golden_boxes`) and `golden_disk_ops` (sizing, root expansion) are separate; import from the owner. `build_golden_set` sequences `_build_unbooted_goldens`, `_clone_missing_goldens`, `_convert_goldens`.

One VM per box type is full-cloned to `engine_vmid + 150 + box_idx`, planted with the golden stage (strict), cloud-init cleaned, stopped, converted.

- **Why the API, not a managed resource**: `qm template` conversion happens outside any resource lifecycle, so a declaratively managed golden would drift the moment it converted. The estate records finished templates in the document's `templates` section and consumes them as linked-clone sources via `golden_template_ids`.
- **Resume routing**: all golden vmids already templates → skip. Some plain VMs → roll `tz-base`-marked ones back and re-plant, clone the missing. A *partially converted* set is fatal (templates can't be un-templated) unless every converted vmid's description hash matches its expected hash (selective rebuild or added box type); then only unconverted slots are worked. Cold (unbooted DC) slots: any plain VM is a dead attempt that may have booted and specialized, so destroy it and full-clone from the generalized base, then convert without ever booting.
- `unbooted_golden_boxes()`: absent `domain_roles.json` = empty set; present-but-invalid raises. Silently widening the booted set would give the DC a booted shared golden, i.e. duplicate DomainSIDs.
- **Windows identity**: only the original templates are sysprepped, so SIDs duplicate across teams and stay unique within a team; phase 6 never re-syspreps.
- Per-box snapshot/password/cloud-init-clean/convert passes use `run_concurrent(max_workers=4)` (each drives a Proxmox task; 4 not 8). Full-clone loops stay serial.
- **Root expansion** runs guest-side before the plant. A Fedora btrfs root reports `/dev/sda3[/root]`; the bracket breaks the partition-digit parse and resize2fs can't grow btrfs, so expansion strips the suffix, grows btrfs natively, and tolerates a late `/dev/dm-N` (`dmsetup mknodes`). It retries, then fails hard only when the root fs is measurably too small; unmeasurable warns.

## Template lifecycle (`engine/template_freeze.py`, `engine_template_ops.py`)

Per competition: build → test runs (reuse; rebuild on config change) → freeze → competition → destroy. Not a cross-competition cache; templates are never patched in place.

- **Hashes**: sha256 per template over only what affects disk contents, stored on the template description (node-side truth) and in the document's `templates` section (which survives a fresh deploy; 0600 — goldens embed `box_password`).
  - *config-class*: base template vmid; golden-stage config list with pinned `{name, vars}`; the per-box `payload_hash` (that box's plan step `script_sha256`s, so a web01 pin change doesn't rebuild app01/dc01/win01); box user/password; SSH key; apt-proxy flag. Engine: base image vmid + pinned `quotient_ref`.
  - *code-class*: disk-affecting function sources (engine: bootstrap + clean step) plus the engine's shape as `estate.apply.create_engine` (field `estate.apply:create_engine`) only, so team-box edits don't rebuild the engine.
  - Excluded: team count/identifiers/IPs, `event.conf`, pre-domain/final configs, snapshot names, mgmt IPs, DB passwords (applied after cloning or cleaned from the template). Going from a 2-team test to an 8-team event rebuilds nothing.
- **Reuse vs rebuild**: the template's own node description (`tezcatlipoca-m4 hash=`) is the authority — stamp matches the current inputs → reuse; differs + not frozen → destroy that template's clones and the template, rebuild; missing → build. Golden granularity is per box type. the document's `templates` section corroborates (it carries the recorded *inputs* the frozen gate classifies drift against); it never decides reuse, because it is per-comp-dir state that the dashboard's workspace seeder strips (`workspaces._STATE_NAMES`) — keying on it alone rebuilt every template a `--teams-only` teardown had deliberately kept, which is what made "test, then deploy the same templates" impossible. Every reuse/rebuild decision goes through `template_freeze.template_stamp_current`.
- **Engine template build**: full clone of the base image → bootstrap at pinned `quotient_ref` (the hash input must be computable before building; the realized HEAD is recorded for traceability only) → clean (compose down -v, remove `.env`/`event.conf`/credlists, truncate machine-id, cloud-init clean, remove host keys; apt-cacher-ng stays installed with an empty cache) → `qm template`. The deployed engine is a linked clone with fresh identity and host keys every run.
- **Frozen**: never rebuilds. Config-class drift hard-fails naming the fields; code-class drift warns and proceeds off the frozen template (a post-freeze log line mustn't break a mid-event rebuild or recovery). For goldens, the pre-phase-1 `golden_freeze_gate` refuses config drift before any Proxmox mutation and warns on code-only drift; phase 4's rebuild gate applies the same verdict before destroying a template. Mid-event rebuilds/recoveries warn when the node's hash differs from the verified record.
- **Freeze** (`verify-competition.py --freeze`): requires all gates PASS incl. plant coverage; records hashes, git commit, dirty flag, timestamp and gate results in the document's `templates.frozen`. `--unfreeze --confirm-unfreeze` is pre-competition only.
- **Full teardown** also drops the hash record (`forget_template_hashes`); a surviving record would "reuse" a hash with nothing behind it.
- **Team rebuild** re-clones from the goldens and runs the post-clone stages in deploy order (pre-domain → domains → final → last), never the full config; planting boot-hostile configs before the joins is the brick hazard the split removed, and the last pass exists because the credlist/seed `useradd` recreates the account files.
- **Engine recovery**: `redeploy_engine_ops._replace_engine` destroys only the engine vmid and re-clones it from the engine template. Scoped by construction — the destroy names the engine and the create builds one VM — so a team box rebuilt out-of-band (which `rebuild` causes by design) is never wiped mid-event.

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
- **`cut_over_engine`/`cutover_netplan_yaml`**: rewrites `/etc/netplan/60-team-ifaces.yaml`: team NICs up with no address (`dhcp4: false` + `optional: true`; an empty entry defaults to dhcp4 and hangs), transit NICs `172.31.<id>.1/30` with the `192.168.<id>.0/24 via 172.31.<id>.2` routes, `netplan apply`, no reboot. NIC naming mirrors the engine's create order (`estate/engine_setup.netplan_yaml`): team NICs `ens19+` in sorted team-key order, transit NICs continue the sequence.
- **`verify_in_path`**: fail-loud gate: WAN SSH, `ip route get` via `172.31.<id>.2`, first managed box reachable through the firewall. Remediation text is kind-aware (`pfctl -sr` vs `show firewall`).

## deploy.py + deploy_lib/

`deploy.py` is a shim (loads `.env` as a plain dotenv layer of `TEZ_*` config overrides, silences urllib3, re-exports `main`/`deploy`/`prepare`/`DeployContext`); `create-competition.py` imports it, so those overrides land in the process environment before any config read. Imports flow one way: `cli` → `runner` → `prepare` → step modules; `phases/` and `context` depend only on leaf helpers. Tests patch via `tests/_deploy_patch.dpatch(name)`, which replaces a name in every `deploy_lib` module that binds it.

| Module | Responsibility |
|---|---|
| `cli` | `build_parser`, competition selection, `write_new_competition`, `print_plan`, `main` |
| `runner` | `deploy()`: lock, `prepare()`, `run_pipeline` (walks `PHASES`, checkpoints, hooks `connect_estate` after phase 2, honors a pause request at each boundary), `report_failure` |
| `pause` | `pause_at_boundary`: a run-queue hold (`TEZ_PAUSE_FILE`) parks the pipeline after the phase in progress, records `paused_after_phase`, and resumes when the request file goes |
| `prepare` | Ordered pre-phase steps filling one `DeployContext`; `None` if the operator declines |
| `context` | `DeployContext`, `save_state`, `checkpoint`, ownership-tag properties |
| `inputs`, `secrets`, `placement`, `configs`, `estate_inputs` | Disk reads; run id/roster/secret mint and carry; placement + endpoint-keyed engine lock; stage configs, golden hashes, frozen gate; `teams.json`, the absolute SSH key path, the document's identity |
| `golden_plan` | `golden_hash_entries`, `golden_rebuild_gate`: the per-box hashes and phase 4's rebuild decision |
| `coverage`, `failure` | Plant-coverage bookkeeping; `record_degradations` (the ledger verify's `degradations` gate reads) |

- **`prepare()`** order: resolve engine vmid; load Compfile/config/state; resume gates; load inputs; resolve teams; compute placement (taking the endpoint-keyed engine lock); resolve the engine mgmt IP; range gates; mint secrets; stage configs + golden hashes + frozen gate; write the estate inputs; confirm; enumerate targets. `gates.check_resume_gates` runs right after prior state is read and `gates.run_range_gates` (stale estate state, then capacity/collision preflight) after placement, both **before the secrets mint**, so a refusal never leaves a credential-desync corpse. `.deploy.lock` is taken by `deploy()`; nothing in `prepare()` destroys anything.
- **State writes**: `save_state` → `persist_state` → the document's `absorb` + `save` (atomic `os.replace`, 0600 at creation, through `config_ops.write_text_atomic`). `checkpoint(n)` sets `last_phase` in the `run` section, the resume guard's only source.
- **Pause at a boundary** (`TEZ_PAUSE_FILE`, set per run by tezcatlipoca-dashboard): the boundary is the only consistent place to stop a deploy — a phase can have a VM build or clone in flight, and cutting one open mid-call leaves a created VM unrecorded (the phase-1 clone-marker reclaim and the resume guards exist for exactly that). The hook runs at the end of each *executed* phase (a skipped phase is no boundary: its checkpoint never moved), writes `paused_after_phase` so an observer sees the hold without parsing stdout, and blocks until the request file is deleted — same process, same lock, resumed in place. `run_pipeline` clears a marker left by a run that died parked, so a resume cannot read as paused. Unset is a no-op, which is what keeps manual deploys unchanged.
- **Resume** (`from_phase > 1`): skips phases 1–2, reloads teams and secrets from state. Only *executed* phases checkpoint. `--from-phase N` with no state file is a hard `SystemExit` (else a fresh branch would mint new passwords and overwrite `teams.json` while skipping phases 1–2). Different `pipeline_version` refuses. `guard_resume_from_phase()` refuses `N > last_phase + 1`. A secret field missing from an older state is regenerated once and written back so a second resume reuses it.
- **Passwords** are generated per run (public repo; fixed literals are guessable). `box_password` is a golden-hash input (baked into `/etc/shadow` and cloud-init on the golden disk), so a fresh deploy carries it forward from prior state, and packet `passwords.json` outranks both; re-minting would rebuild every golden.
- **Bundles** are built operator-side (the only vulndb-dependent steps): three stage bundles, so the engine never sees vulndb credentials and no full-machine pass is deployed.
- `all_targets` is built once from the full box list; nothing may re-derive a vmid from a filtered list. `teams.json` is persisted because destroy and verify read it.
- A fresh deploy unlinks `.postclone-swept`; the phase-4 clone pass unlinks it again after re-creating team boxes so a phase-6 resume always re-sweeps fresh clones. `confirm_deploy()` is skipped on resume and by `--yes`.
- **Phase 1**: reclaims the untagged interrupted clones (ownership `MARKER`) at this deploy's slots, then resets the comp dir — `reset_domain_markers()` clears `.nakon-domain-*` so a stale marker can't skip a promotion, `.postclone-swept` and the previous teardown's journal go too. No VM a run owns is touched: preflight refuses a leftover range and `destroy-competition.py` reclaims it.
- **Phase 2**: `create_engine_and_bridges` runs its creates serially (one API writer per node; the datastore-saturation guard `create_team_boxes` documents is for bulk clone writes). Engine SSH is polled (`wait_for_ssh`) and `forget_engine_host_key` drops the stale pin. Records `deployed_endpoint`.
- **Phase 3**: `event.conf` before any nakon run prevents the Quotient crash loop that wipes NAT.
- **Phase 4**: after the golden set and the team-box clone pass: Windows bootstrap for all teams, concurrent waits, `setup_ubuntu_auth`, DNS fix, apt prep, `tz-base`. Clones boot with working DNS/apt because the golden disk carries no disruptive configs.
- **Phase 5**: print-and-return without an `in_path` box (keeps phase indexes stable). Order is load-bearing: the engine owns bridge addressing while configs are pushed, and phases 6–7 mustn't run before cutover. Records `state["firewalls_bootstrapped"]`; resume re-pushes (unchanged config isn't reapplied). Firewall `tz-base` is taken after cutover.
- **Phase 6**: `.nakon-predomain.json`, lenient (`strict=False`: one flaky plant mustn't kill a sweep that's 98% landed), `--jobs N`, gated by `.postclone-swept`. `fix_services_on_boxes` runs after it (the ssh-\* configs restart sshd and can trip start-limit).
- **Phase 7**: domains run concurrently per team; then the final pass; then **red** (`red/tlaloc_ops.plant_assume_breach`) — red01, the C2, the payload freeze and the day-0 seed — before `tz-ready`, so the restore points carry a compromised range. Every red step raises; a range with no working red team is a failed deploy. The account-file mode pass runs last — after phase 6's credlist `useradd` and after the red seed — then the trail wipe, then the snapshot. A Compfile that still sets `team_beacons` is a hard error (the earlier agent's beacon option). `nakon_failed_steps` is merged across passes (deduped, capped at 40).
- **Phase 8**: Quotient is polled (`/api/login`). Sub-steps are flag-gated because `unpause_engine` isn't idempotent; in the crash window between POST and flag-save it asks the engine first (`engine_paused`) and re-POSTs only if still paused.
- **Failure handler**: names the resume phase. An "already exists" error at phase ≥2 means something a prior attempt created still occupies a slot: a leftover range is a preflight refusal now, so reclaim it with `destroy-competition.py` and deploy fresh (a `--from-phase 1` re-run refuses the same leftovers), or resume the run that owns it.
- `credentials.txt` (0600) is the authoritative record of generated secrets (incl. `box-login`, `box-credlist-*`). `--plan-only` exists because nothing otherwise confirms between the box picker and a real deploy. `--competition` skips straight to `deploy()` but boxes are still interactive.

## pipeline_api.py

The import surface for the hyphenated entry points. Explicit `__all__`, each name imported from its owning module, no re-export chains; add a name only when a consumer needs it, so the consumer set stays visible.

## Teardown (`teardown/`)

One engine: `infra.ownership.Ownership` (destruction identity), `Refusal` (saw/expected/remedy, printed as a `TEARDOWN-REFUSAL` JSON line), `Inventory` (classified estate view, fail-closed on listing errors), steps with declared failure policies (`REFUSE` aborts; `WARN_AND_PROCEED` records), `TeardownPlan` (evidence-before-destruction validated at construction), `TeardownEngine` (gates → completion journal → steps → `verify_clean`, the only exit-0 path). Mechanics are in `teardown/machinery.py` (pre-stop, the run-id-scoped leftover sweep, the vmid-math template fallback, gates); the recorded estate itself is destroyed by `estate/destroy.py`. `destroy-competition.py` and the scrim harness (via `teardown/runner.run_terminal_teardown`) are shims, and the terminal plan is the only kind: deploy phase 1 composes no plan at all (a deploy destroys nothing a run owns) and uses `ownership_verdict` directly for its interrupted-clone reclaim. Tests patch `teardown.machinery`, `artifacts_ops`, `red01_ops`/`tlaloc_ops`, `infra.ownership`, `teardown.runner`.

Exit 0 = `verify_clean`: plan completed, nothing of ours running or tagged (teams-only reports kept templates as kept-by-design), red01 gone, evidence archived, no node unlistable. Order: portal → engine capture → evidence → red01 → pre-stop → estate (the recorded inventory, in `estate/destroy.py`'s `TIERS` order) → remote access. `EngineCaptureStep` records the engine's final scoreboard into `<comp_dir>/evidence/` when the harness never captured one (that directory becomes the collector's engine source); the scoring DB does not survive `EstateStep`, and a harness capture already present is never overwritten. A lost evidence item is a `verify_clean` NOTE, never a problem: exit 0 still means "the estate is clean", but a lossy teardown says so where the operator reads it. The template step is `full`-gated: `destroy-competition.py --teams-only` and the harness's `run-agent-scrim.py --keep-templates` both reach it with `full=False` (the harness keeps `keep_templates` in the run manifest, OR-semantics, so a resume cannot drop the intent the first run had).

- **Ownership**: `run_id` anchors everything; `load_ownership` refuses without it. Pre-stop stops only name-matching VMs that also carry the ownership tags; `sweep_tagged_leftovers` needs the full set; golden/engine/jump destroys pass the role-narrowed set.
- **Orphan reclaim** (`teardown/orphan_ops.py`): for a run whose comp dir is gone, the caller names the comp + run tags and the VMs' own tag set is the proof — the only path that needs no state. It refuses the clone-marker adoption every other path allows (`destroy_vm_if_exists(..., require_owned=True)`: the marker names the competition, not the run), requires `--allow-templates` for golden/engine-template VMs (no freeze record survives to consult), refuses when a comp dir still holds the run id, and takes out a bridge only when its PVE comment proves this competition and run id (a bridge carries no tags, so the comment is its only identity; a foreign or uncommented bridge is named and left alone). Once nothing of the ownership set is left it retires the range in the dashboard store (`POST /api/ranges/retire`), because a reclaim is not a run and nothing else would tell the board. The dashboard address and bearer are the run-queue handoff (`TEZ_DASHBOARD_HOST`/`TEZ_DASHBOARD_PORT`/`TEZ_DASHBOARD_TOKENS`, inherited from the service), never estate config; an unset, refused or unreachable handoff only prints the `curl` to make, never failing the reclaim.
- **Golden wave**: `EstateStep` destroys the recorded golden tier right after the team-box/jump tier. A full teardown whose document records no `golden`/`engine_template` resources falls back to `teardown_templates`, which API-destroys goldens by vmid math (`engine_vmid + 150 + slot*10 + i`) with the ownership check.
- The recorded-estate destroy walks `TIERS` serially; the run-id-scoped leftover sweep behind it uses `TEARDOWN_WORKERS = 4` (deletes are metadata-light; bulk clone writes stay serial).
- `load_destroyable_competitions()` requires `Compfile` + `teams.json` + `boxes.json`.

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
| `context`, `loaders`, `boxes`, `creds` | `read_estate_ctx` (the engine's coordinates from the state document), ssh helpers, `REPO_ROOT`; comp-dir readers; machine predicates; admin-password lookup |
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
- Remote loops (`check_domains`, misconfig probes) use the 8-worker pool (pure SSH/agent probes) and re-serialize aggregation, so output and exit codes are unchanged (`tests/verify/test_parallel_verify.py`).

## generate-packet.py

Local-files → Markdown. The packet is the same for every team (real credentials are issued at start via `credentials.txt`) and never reads `box_vulns.json`. `_SERVICE_DISPLAY` mirrors `quotient/setup.py`'s Display fields as a smaller separate table (no dependency on Quotient's TOML shapes). `load_inject_schedule()` emits titles and offsets only.

## packet_ops / compile-packet / run-schedule

- `validate_profile()` surfaces everything that would die mid-deploy at compile time (comp-name/unix-username validity, legacy-account collisions, domain `name_template`, credlist/domain cross-requirements, box count/name/octet/template/fidelity/role rules, exactly one DC, pin and display uniqueness, inject offsets). Template validity is only "non-empty"; resolution is the preflight's job.
- Emits `Compfile` (with `domain_prefix`/`domain_suffix`/`packet_source`), `boxes.json`, `users.json`, `box_services.json`, `domain_roles.json`, `injects/`, `packet-fidelity.md`, and 0600 `passwords.json`, `domain_accounts.json`, `box_baseline.json`.
- **Score-only pins** (`score/tcp`) score a native service the catalog can't plant; they're stripped from the nakon machine list and catalog check but present in `event.conf` and `expected_service_names`.
- `run-schedule.py`: the engine has no schedule model; pausing is the schedule. With `--execute` it drives `start`/`freeze`/`resume`/`end` from the packet's `schedule:`; `end` captures the scoreboard and injects into `<comp>/evidence/` before pausing.
- **Packet sourcing**: packets live in the comps repo beside their comp definitions (`<comps>/cde-2026/packet.yaml`, or a colocated `packet.yaml` inside the comp dir); `resolve_packet_source()` is the one resolver — comp-dir-relative first, then comps-root-relative — behind the Compfile's `packet_source`, verify's `--packet`, and the CLI profile args.

## Multi-node and sync

[multi-node.md](multi-node.md) is the full treatment.

- Placement is capacity-fill. Per-host tokens are referenced by env-var name (`token_env`). `activate_placement()` points the process config at the engine host (in-process `config.active().activate(...)` overrides, not an `os.environ` mutation) and registers each node's endpoint/token route; `deactivate_placement()` restores both. The document's `placement` section is authoritative; a resume without one adopts its recorded `deployed_endpoint` as a single-slot placement. `golden_vmid_for_slot()` shifts the golden block one box-stride per satellite (`engine_vmid + 150 + slot*10 + box_idx`) because linked clones can't cross hosts; `jump_vmid_for()` is `engine_vmid + 130 + slot`.
- `net/jump_ops.py`: per-satellite Alpine VM impersonating the engine at L3 on the satellite's bridges (holds `192.168.<id>.1`), DNATing the gateway IP's apt-cacher port to the engine and SNATing engine→box traffic so boxes' gateway-only SSH trust holds. Forwarding is default-DROP with explicit accepts. NIC hotplug past the first is unreliable: reboot instead.
- `routing_ops.verify_satellite_routing()` is the fail-loud gate after the engine create. The routes come from `estate.engine_setup.satellite_route_commands` (`satellite_routes_for(placement)`, plus a oneshot unit so an engine reboot re-asserts them); this module only proves them.
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

## red/ and tlaloc/

One red engine, owned by this tree. There is no sibling checkout and no `red.engine` switch: tlaloc is
the only engine, and the range side of red is a component like the engine and the team boxes.
Three modules: `red/red01_ops.py` owns red01 and its engine-side rules, `red/tlaloc_ops.py` is
deploy phase 7, `red/gaia_ops.py` installs Mythic. The agent itself lives IN this tree at `tlaloc/`
(package, tests, docs, systemd unit); its own map is `tlaloc/README.md` and `tlaloc/AGENTS.md`, and
the deploy contract is `tlaloc/docs/deploy.md`.

**`red01_ops.py` — the VM, its network, the engine rules, teardown**

- **Identity is reserved per competition.** vmid: Compfile `red01_vmid`, else the placement's
  recorded `red01.vmid`, else one golden stride past this placement's last golden band
  (`engine_vmid + 150 + (satellites + 1) * 10`). IP: Compfile `red01_ip`, else the placement's
  recorded address, else selected immediately before the clone from `10.0.0.240-10.0.0.249`
  (`infra/collisions.select_red_ip`: the lowest address no running guest answers and the placement
  does not already claim; the legacy `.196`-`.199` values are excluded). The selection is recorded
  for the run — written back into the placement, or into the deploy's `red` summary on single-node.
  `infra/red01.py` preflight-gates the slot: an occupied vmid is a refusal
  naming the owner (another run of this competition vs. a foreign VM), unreadable tags are refused
  too, and an address a running guest answers is refused.
- **The VM** is a linked clone (`full=0`) of the fixed `base-ubuntu24.04-fix` template (a
  qemu-guest-agent + cloud-init Ubuntu; there is no per-comp template knob), named `tlaloc-red01`.
  The clone POST carries the competition's clone marker, then the config PUT lands the run's full
  tag set, `onboot`, the guest agent, cloud-init user (`sysadmin`) + the deploy's public key,
  `net0` on the placement's management bridge and `net1` on `red_bridge_name(run_id)`
  (`vmbrR<hex>`, this run's private red-segment bridge). A resume adopts a VM already carrying this
  run's tags and reconfigures it; a VM in the slot without them is a refusal, never a reconfigure or
  destroy.
- **Network is pinned by netplan, not cloud-init.** `/etc/netplan/51-tlaloc.yaml` (0600) sets the
  management address/gateway on the first NIC and `RED_SEGMENT_IP` (`10.200.0.10/24`) on the second,
  with `192.168.0.0/16 via RED_SEGMENT_GW` (`10.200.0.1`);
  `/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg` disables cloud-init networking — a
  kernel-upgrade reboot re-applied cloud-init and came back with no IPv4 at all.
- **Engine rules** (`install_engine_rules`; installed even when `red.c2 off`, because the segment
  rules are not optional): `/usr/local/sbin/range-red-firewall.sh` plus
  `range-red-firewall.service`/`.timer`, `After=docker.service`, re-asserted every 30 s. The unit is
  separate from `range-firewall.*` because Docker wipes iptables on a container restart. The script
  is a routed-segment FORWARD trio in load-bearing order (red→`192.168.0.0/16` ACCEPT, return
  `RELATED,ESTABLISHED` ACCEPT, DROP toward red) and, when a callback port is known, a
  per-team-gateway DNAT (TCP+UDP) to `10.200.0.10:<port>`, a FORWARD accept for it, and a nat
  POSTROUTING ACCEPT that exempts the flow from the range's team SNAT (so blue does not see the
  check-in sourced from the gateway). `remove_engine_rules` is best-effort teardown of the unit and
  its FORWARD rules: the engine survives a teams-only teardown, so the red segment must not outlive
  its red01.
- **Teardown** destroys through `infra.ownership.destroy_vm_if_exists` with `expect_tags` = the
  run's full tag set, so a recycled vmid or another run's red01 is refused rather than deleted. The
  identity is read from the placement/state (`recorded_identity`), never re-derived at teardown.

**`tlaloc_ops.py` — phase 7**

- **The `red.*` block** comes from the Compfile (`red_yaml <file>` may point at a YAML body;
  `red.*` dotted lines override it). `validate_red_block` checks `red.assume_breach full|light|off`
  (default `full`), `red.depth 0..3` (default 3), `red.duration_min`, the `red.ladder` tiers
  (`at_pct`/`at_min`, sorted by time, known takedown modes) and `red.caps.min_standing >= 1`.
  `red.disk_gb`, `red.clef_url`, `red.c2_url`, `red.c2_callback_port`, `red.c2_ip`, `red.c2 off`,
  `red01_vmid` and `red01_ip` are the engine/VM knobs; `red.windows` and `red.budget` travel to
  tlaloc with the rest of its block.
- **The config**: `.tlaloc-config.yaml` in the comp dir is rendered from the block and pushed to
  `/etc/tlaloc/config.yaml` (0600). Only values the deploy knows are written, so the pushed file is
  a diff against tlaloc's documented defaults; `verify_config_contract` fetches it back and diffs it
  against `tlaloc verify-config --print-contract`, refusing when a field the deploy MEANT to set is
  absent (that feature would run on defaults on the VM while every local test stays green).
- **Order, every step fatal** (a range with no working red team is a failed deploy):
  render config → provision red01 (`red01_ops.deploy`, then `record_identity` into the placement) →
  grow the disk to `red.disk_gb` (default 60G; Mythic's images filled a 9.8G disk, Docker never
  started and the channel had nowhere to call home) → unless `red.c2 off`: install Mythic via Gaia
  and freeze the payloads (`tlaloc c2-prep`) → install the engine's red rules (the DNAT only when
  the C2 supplied a callback port) → stage the agent to `/opt/tlaloc` (package, unit,
  `config.example.yaml`, plus the comp's intel files) → push the config and prove the contract →
  install and enable `tlaloc.service` → run the day-0 seed on the box (`python3 -m tlaloc --config …
  --comp /var/lib/tlaloc/intel seed`) → pull the seed manifest to `<comp>/red/manifest.json`. A
  scrim-supplied LLM key rides a 0600 `/etc/tlaloc/env`. Staging, not installation, is deliberate:
  red01 has no pip, and the unit is what puts the package on the import path. `scrim/red_stage.py`
  calls the same `plant_assume_breach`, so the harness and the deploy cannot drift.

**`gaia_ops.py` — Mythic via Gaia**

- The Gaia checkout is on the deploy host at `knobs.gaia_path` (empty → `~/gaia`); the step clones
  it at `main` if missing, builds its venv from `requirements.txt`, and runs every `gaia.py`
  command with the checkout as CWD (Gaia reads its `.env` relative to the working directory).
  Commands: `install` (apt/deps/Mythic), `auth` (API key; the password goes in on stdin), `user
  create` for the agent's own `tlaloc` account, `operation create` for the competition.
- The admin password's authority is the C2 box's own `/opt/Mythic/.env`, not the handoff file an
  installer re-run can leave stale; the handoff file is corrected when the box disagrees. `auth`
  runs before `user`/`operation` because those speak through the API key it writes, and a
  just-installed web UI that is up but not yet serving JSON gets retried before the step calls it
  broken.
- The agent's config gets `c2.*` with `password_env` (never the password); the password travels in
  a 0600 root `/etc/tlaloc/c2.env` that `tlaloc.service` reads. `c2_plan` puts the C2 on red01
  unless `red.c2_ip` names a box, makes the callback host the first team's gateway
  (`192.168.<id>.1`, or `172.31.<id>.1` behind an in-path firewall) and the port
  `red.c2_callback_port`, else the Compfile's `firewall_dnat` port. A competition with no derivable
  callback port refuses before installing — the frozen payloads would dial nothing and the channel
  could never check in. `red.c2 off` skips the Mythic install and the payload freeze; the engine's
  segment rules are installed either way.

**Verdict and evidence**

- Before red01 is destroyed, `destroy_tlaloc_red` runs `tlaloc report` on the box, tars
  `/var/lib/tlaloc` into `<comp>/red/` and fetches the seed manifest there — `report.md`,
  `restore-table.json`, `report-secrets.md`, `world.json`, `events.jsonl`, `manifest.json` — then
  writes `verdict.json` (engine, entry/command counts, restore-table summary, report path). Only
  then does it stop the unit and destroy the VM.
  A failed capture warns and teardown proceeds — the box still holds the record, but destroying it
  gives up the last chance to pull it. `collect_evidence` copies the pulled files into an evidence
  tree.
- Artifact collection is the other copy: `artifacts_lib` sudo-stages those files into `/tmp/ba`
  (the originals are 0600 root), pulls `journalctl -u tlaloc`, and collects them into the run's
  evidence before the teardown's red step.

## estate/

The one component that mutates real infrastructure. Each creator is an ordinary function that calls
the Proxmox API and records what it made in the document's `resources` inventory, which is what the
destroy path walks. Creating is a call in the phase that needs it, and a re-entry adopts an
already-created resource instead of re-planning; nothing runs a resource out of band.

| Module | Owns |
|---|---|
| `state` | The `estate-state.json` document and its API; the `resources` inventory |
| `config` | Native process config (`tezcatlipoca.toml` + env) and the `_SPEC` `key -> default` table |
| `apply` | Bridge, engine and team-box creates; disk sizing; discard |
| `engine_setup` | The engine's cold boot, guest netplan/forwarding/satellite routes, SSH coordinates |
| `destroy` | `TIERS`, `destroy_estate`, `destroy_tier` |

**state.py** — `competitions/<id>/estate-state.json`, `schema_version` 1, written atomically at 0600.

- Sections: `identity`, `secrets`, `roster`, `placement`, `templates`, `resources` (the inventory), `run`, `external`, `teardown`, plus `updated_at`. `_FLAT_SECTION` maps the deploy's flat state keys onto those sections, so `absorb`/`flatten` round-trip the dict the phases already pass around; `run` is the catch-all. The document holds the only copy of the box and team passwords, so a corrupt file raises rather than resetting, and a `schema_version` above this code's refuses with the upgrade path.
- API: `state_path`, `EstateState.load/flatten/absorb/save/section/record/resources/forget/checkpoint`, `load_flat`/`read_flat` (`{}`/`None` when absent), `write_flat`, `load_all`, `team_vmids`. `read_flat` reads only `estate-state.json`, so a competition with no document reads as no state.
- `record(kind, node, name, vmid=, bridge=, tags=)` upserts one inventory entry, keyed by vmid, else bridge, else name; `resources(kind, node)` is the filtered read; `forget` drops what a destroy removed. Teardown walks this list, and `team_vmids` is the resume gate's "is the work there" probe (`infra.gates.guard_resume_existence`).
- The document is the only source and the only writer: each owning module reads its own section — `multinode/placement_record` the `placement` section, `common/targets` the `roster` section, `engine/template_freeze` the `templates` section, `teardown/engine` the `teardown` journal.

**config.py** — the native config surface for the deploy's process settings (`tezcatlipoca.toml` and its `TEZ_*` env overrides).

- A `Config` over `tezcatlipoca.toml` (`TEZ_CONFIG` overrides the path), resolved per key as override → env → file → default (env beats file because the dashboard injects per-run node tokens and the engine mgmt IP into the process environment). `_SPEC` is the one place the key set lives, as `key -> default`, and it is the operator-facing set; names the code reads that are deliberately not operator config (the dashboard's `TEZ_PAUSE_FILE`/`TEZ_DASHBOARD_*` handoffs, the portal's `TEZ_OPS_*`) are allowlisted with a reason in `tests/hygiene/test_secret_hygiene.py`'s `INTERNAL_ENV`, not declared here. `config.env_name(key)` is `TEZ_` + the key upper-cased with dots as underscores and the `knobs.` prefix dropped, and the default's type drives parsing. `config.require(key)` raises on an unset key; `Config.is_set` distinguishes unset from deliberately empty (an empty engine mgmt IP means DHCP, not "re-default me"). `knobs.gaia_path` names the Gaia checkout the Mythic step runs from (empty → `~/gaia`).
- `Config.activate`/`deactivate` are in-process overrides, which is what lets multi-node placement point the process at one host (`multinode/nodes_config.apply_node_env`) without mutating `os.environ`.
- `ssh_key_path()` resolves a relative private key against the checkout root.

**apply.py** — the creators (the bridge/engine/team-box resources as API calls).

- `create_bridge`: POST a Linux bridge with `bridge_ports=none` (isolated, no uplink), then a PUT that applies the staged network config — a VM cannot attach to a bridge that is only staged.
- `create_engine`: linked-clone the engine template (`agent: 1`, cores/memory/disk from `ENGINE_*`), then PUT the NIC list in order — `vmbr0`, one per slot-0 team bridge, then the transit bridges, then the private red bridge LAST (so the netplan's `ens19+` numbering is unchanged) — because the engine's netplan names interfaces positionally. Cloud-init identity (`ciuser`/`sshkeys`/`ipconfig0`) only when a static mgmt IP is set, since the template's netplan and baked key/sudo suffice otherwise. `ensure_disk_size`/`set_discard` follow, then start.
- `create_team_box(es)`: linked-clone the source vmid the phase passes (each box's golden, or its own base template when unmanaged); the in-path firewall's WAN NIC comes FIRST (pfSense keys its interfaces on `vtnet0`/`vtnet1`), then the team-bridge NIC. Cloud-init identity only for a managed, non-Windows box, with `nameserver 8.8.8.8` — never the team's own `dns*` box, whose bind9 is installed by nakon over apt and needs a resolver that already works. Clones are serial on purpose: one API writer per node is the shape this estate is proven at, and the datastore saturation to avoid is bulk clone writes.
- `ensure_disk_size` grows the root disk only when `disk_gb` is set (a shrink destroys data); `_root_disk` finds the key by scanning `scsi*`/`virtio*`/`sata*`/`ide*` and skipping cdrom/cloud-init, so a SATA-booting Windows template resizes the same way. `set_discard` adds `discard=on` to the inherited clone disk.
- Every create is idempotent through `_adopt`: a VM carrying this run's full tag set is adopted, one carrying only this competition's clone marker is an interrupted clone and is rebuilt, anything else is a `Refusal` naming the remedy. The clone POST writes the clone marker into `description` because `/clone` takes no tags and the tagging PUT only lands after the task finishes. `_configure` retries the brief post-clone 400 window (caught live: three deploys died on it). There is no per-VM readiness cap here: the phases' `wait_*` helpers own waiting.

**engine_setup.py** — the guest-side engine setup and the cold boot, as ordinary calls the phase makes in order.

- `cold_boot`: hard-stop, settle, start. A guest reboot does not trigger the PCI rescan that reveals newly added VirtIO NICs, so this must run after the NICs are in the VM config and before the netplan that addresses them.
- `configure_engine_network`: write `/etc/netplan/60-team-ifaces.yaml` (`ens19+`, `192.168.<id>.1/24`; transit NICs `172.31.<id>.1/30` appended after every team NIC), `chmod 600` (netplan refuses a world-readable file), `netplan apply`, set `ip_forward=1` + `rp_filter=2`, then install the satellite-route oneshot unit so an engine reboot re-asserts its routes. The netplan block is omitted when the engine hosts no slot-0 teams — an empty `ethernets` map is a netplan error.
- `wait_engine_ready` polls SSH then settles; `engine_ctx(comp_dir)` is the SSH-coordinate dict (`scoring_engine_ip`, `ssh_key_path`, `vm_username`) read from the document and exported through `pipeline_api`; `verifier.context.read_estate_ctx` builds the same dict for verify, so both dial the same coordinates.

**destroy.py** — the one order linked clones survive.

- `TIERS = (("team_box", "jump"), ("golden",), ("engine",), ("engine_template",), ("bridge",))`. A linked clone shares its base volume, so destroying a golden or the engine template first fails with `base volume ... is still in use by linked clone`, and a bridge cannot go while a VM NIC still references it. `TIERS` is the only thing that decides the order.
- `destroy_estate(state, full=, nodes=)` walks the tiers; `destroy_tier(state, kind, only=)` narrows to a set of vmids (or bridge names) for a per-team teardown. Each entry goes through `destroy_vm_if_exists`/`destroy_bridge_if_exists` under the role-narrowed ownership set (`golden`/`engine_template` add their tag; the deployed engine is an ordinary clone proven by the full ownership set alone). A `Refusal` leaves the entry in the inventory and carries on with the tier — a refusal is a verdict, not a step failure.
- `full=False` skips the `golden` and `engine_template` tiers, which is the teams-only mode.

**Inputs** — `box_username` is themeable via `users.json`; `box_password` is generated or carried (nakon uses password auth). `collect_teams` walks identifiers from 101 up (packet and prior-state suggestions first), matching the engine's own rules. `disk_gb` omitted keeps the template's size; the clone itself is created on `proxmox.datastore`. `template` must match a `_template_vmid_map` key exactly, and that map excludes running VMs (a running box with a stray `template` tag duplicated names and failed every apply). `engine_mgmt_ip` defaults to the static `DEFAULT_ENGINE_MGMT_IP`, and an explicit empty value restores DHCP. Others: [usage-people.md](usage-people.md#configure-the-event).

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
