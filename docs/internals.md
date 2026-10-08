# Internals — per-module design notes, invariants, and rationale

The "why" behind individual symbols, constants, timeouts, orderings, and workarounds — largely the
content that used to live only as comments and docstrings beside the code (much of it was stripped
from the code in `af7468c`, so deleting a note here is not always recoverable from source). Verified
against the tree 2026-10-02; re-verify before acting on a line, especially timeouts and symbol names.

[architecture.md](architecture.md) owns the system-level view (overview, the eight phases, the
component map, data flow, network/isolation, the snapshot table, the nakon contract, the secrets
inventory, the stage-split rationale, and the state-file schemas); this doc deliberately does not
repeat it. Only the design
consequence is repeated here. The agent-scrim trio (`run-agent-scrim.py` + the `scrim/` package, `scrim-report.py` + the
`scrim_report/` package, `beacon_ops.py`) is documented in [scrim-harness.md](scrim-harness.md)
(module map at the top), not here.

`constants.py` · `utils.py` · `ssh_ops.py` · `range_ops.py` · `config_ops.py` · `nakon_ops.py` ·
`engine_ops.py` · `windows_ops.py` · `hardening_ops.py` · `golden_ops.py` · `template_ops.py` ·
`domain_ops.py` · `timing.py` · `deploy.py` · `deploy_lib/` · `pipeline_api.py` ·
`create-competition.py` · `destroy-competition.py` ·
`redeploy-competition.py` · `verify-competition.py` · `generate-packet.py` ·
`packet_ops`/`compile-packet`/`run-schedule` · multinode & sync · `quotient/setup.py` ·
`terraform/main.tf` · `terraform/variables.tf` · Conventions

## constants.py

Single source of truth for VM identity math, snapshot names, nakon budgets, and naming; shared by
`range_ops`, `deploy`, `redeploy`, `verify`.

| Symbol | Value | Why |
|---|---|---|
| `SCORING_ENGINE_VMID` | `1000` | **Default only** — `--scoring-vmid` / `TF_VAR_scoring_vm_id` overrides it (deploy exports it into tfvars; `main.tf` uses `var.scoring_vm_id`). 100 collides with an unrelated VM on the primary node. |
| `DEFAULT_ENGINE_MGMT_IP` / `_GW` | `10.0.0.250` / `10.0.0.1` | Static by default: a DHCP engine rebooted onto a different address mid-event while terraform's saved output stayed stale (shakedown-5x4: .221→.243→.233). High in the lab's `10.0.0.0/24`, clear of the red01 slots (.198/.199/.244) and the observed drift. |
| `MAX_BOXES_PER_TEAM` | `10` | Box index 0–9 fits the stride-10 block each team owns. |
| `MAX_TEAMS` | `154` | Identifiers 101–254 map to `192.168.101.0/24` … `192.168.254.0/24`; `deploy()` validates `--teams` against it. |
| `SNAP_BASE` / `SNAP_READY` | `tz-base` / `tz-ready` | The two snapshot names every consumer keys on. |
| `PER_MACHINE_NAKON_BUDGET` | `2400` s | `run_nakon` timeouts are `max(2400, budget * machine_count)`. |
| `SLOW_SERVICES` | `splunk`, `roundcube` | Excluded from auto-randomize (`--exclude`); both heavy installs. |
| `DISRUPTIVE_CONFIGS` | `resolv-conf-null-dns`, `apt-sources-empty`, `apt-hold-all-packages`, `dpkg-broken-hold-state` | Break DNS/apt, so they sort last within a pass. |
| `DOMAIN_INFRA_CONFIGS` | `ADDS`, `Domain Join`, `domain-join` | Scored but never nakon-planted from `box_services.json`: `domain_ops` injects them per team with the per-team domain/DSRM vars a golden-stage plant cannot carry. A bare ADDS on a machine list would run dcpromo without them. |
| `PIN_CHECK_OVERRIDES` | `display`, `port`, `path`, `scheme`, `status`, `credlist` | Per-pin scoring overrides for dict-form pins. Quotient requires unique `<box>-<Display>` names, so `display` is how same-TYPE pins coexist; `credlist` swaps which credlist file a check authenticates with. `nakon_ops` strips them from machine lists. |
| `REPAIR_STAGE_CONFIGS` / `FINAL_STAGE_CONFIGS` / `POST_CLONE_CONFIGS` | sets | Stage membership — see the canonical three-pass rationale in [architecture.md](architecture.md#data-flow) and the per-config history comments in the file. |
| `REQUIRED_VARS` | map | Curated because the catalog DB carries no required-vars metadata (see `template_ops`). `"ip"` = machine identity, auto-filled per machine and banned from the golden stage; `"ip:<box>"` = the same team's copy of another box; `"literal"` = operator must pin `{"name", "vars"}`. |
| `GOLDEN_VMID_OFFSET` / `GOLDEN_IP_BASE` / `GOLDEN_TAG` | `150` / `240` / `tezcatlipoca-golden` | Golden vmids sit just above the engine's slot; IPs sit above the `.1` gateway and below `.255` on team1's subnet, free because goldens are long converted before real team boxes exist. |
| `ENGINE_TEMPLATE_VMID_OFFSET` / `ENGINE_TEMPLATE_NAME` | `140` / `engine-template` | Just below the golden block so one preflight scan covers both. |
| `GOLDEN_CLONE_TIMEOUT` | `5400` s | A 60 GB Windows full clone measured 13 min and >30 min on HDD-ZFS; the 1800 s default aborted a healthy clone (live-found 2026-09-30). |
| `WINDOWS_ADMIN_USER` | `Administrator` | The sysprepped local admin nakon authenticates as. |
| `NAKON_DIR` | `vendor/nakon` | nakon subprocesses run with this as cwd so nakon can read its gitignored `.env`. |

## utils.py

Compfile/`users.json` loading, DNS-fix command strings, the concurrency helper. No Proxmox access.

| Symbol | Note |
|---|---|
| `DNS_FIX_CMD` | cloud-init ignores `dns.servers` on a static IP, so the cloud-init block alone does not stick. Repairs live via `/etc/resolv.conf` + `.head` + a `systemd/resolved.conf.d/upstream.conf` drop-in + `systemctl restart systemd-resolved`, all at 8.8.8.8. |
| `DNS_FIX_CMD_ROOT` | Same fix with `sudo ` stripped, for the guest-agent path (already root): needs neither working sudoers nor a network — the whole point of the fallback. |
| `BOX_USERNAME_DEFAULT` / `CREDLIST_USERNAMES_DEFAULT` | `ubuntu` / `["admin","user1","user2"]` — fallbacks when no `users.json` exists; themeable per competition since. |
| `load_users_config()` | Box login + 3 credlist usernames from `competitions/<id>/users.json`, falling back per field. |
| `load_compfile()` | key=value parser; skips blank lines and valueless keys; non-numeric `difficulty` degrades to 0 rather than raising. |
| `compfile_flag()` | Integer Compfile knob reader (e.g. `team_beacons 1`, `golden_boot_smoke 0`, `round_loop_guard 1`); returns the default when the key or file is absent, so old Compfiles keep their behavior. |
| `round_loop.py` | The ONE definition of "the scoring loop is stopped" (`round_loop_state`/`is_stale`), shared by `verify-competition.py`'s gate and the engine-side actor `tools/round_loop_guard.py`. Extracted 2026-10-02 so the unattended actor could not drift from the gate it is duplicating. |
| `tools/round_loop_guard.py` | The actor, installed on the engine as `/usr/local/sbin/round_loop_guard.py` + a 60s systemd timer when the Compfile sets `round_loop_guard 1`. Logs in as the dedicated `scoring` account and issues the same two POSTs verify's `--fix-round-loop` does. Stdlib only (the engine image has no guaranteed `requests`). |
| `run_concurrent(items, fn, max_workers=8)` | The one thread-pool helper. Returns an **index-aligned list** (each slot holds fn's value or the exception — never raised here); an earlier dict-keyed version broke on unhashable items and was live-fixed 2026-09-24. Per-box budgets are independent; prints go through `PRINT_LOCK`. Aggregate semantics: `wait_for_boxes_ssh`/`fix_dns_on_boxes` abort only when **all** boxes fail; `prep_apt_on_boxes`/`fix_services_on_boxes` warn per box; `setup_ubuntu_auth` raises if any box ends without working auth (all boxes still attempted, for diagnostics). Pool cap 8 sits well under the engine's raised sshd `MaxSessions` 64. `fix_services_on_boxes` keeps per-box script *construction* serial, parallelizing only execution. |
| `pick_competition()` | Shared interactive picker used by destroy/redeploy. |

## ssh_ops.py

`quote_sshkeys()` (URL-encodes the key for Proxmox's `sshkeys` param) is defined here once; `golden_target_ops._quote_sshkeys` is an alias and redeploy rebuild imports it.

Gateway SSH (`ProxyCommand -W`), Terraform context, and wait helpers. Shared by create/redeploy/
verify (`verifier/context.py` imports `gateway_proxy`/`engine_ssh_opts` from it) — single source of truth.

| Symbol | Note |
|---|---|
| `read_terraform_ctx()` | Parses `agent_context` out of `terraform output -json`; resolves a relative `ssh_key_path` to absolute relative to `terraform/` (the provider's working dir). |
| `ssh_via_gateway()` | Reaches team boxes through the engine with `ProxyCommand ssh -W %h:%p`. Requires `AllowTcpForwarding=yes` on the gateway (engine_ops sets it). Linux boxes authenticate with the Proxmox key as `box_username`; Windows uses password auth. |
| engine multiplexing (M1.5) | `engine_ssh_opts`/`_engine_opts(host=…)` add `ControlMaster=auto` + `ControlPath=~/.tezcatlipoca/cm-<ip>-22-%r` + `ControlPersist=900`, so every per-box ProxyCommand and direct engine call reuses ONE authenticated connection instead of a fresh double handshake. Two coupled requirements: `forget_engine_host_key` retires the master socket (a surviving master keeps the old host key after a rebuild), and engine_ops raises sshd `MaxSessions` to 64 (the default 10 would throttle the concurrent waits sharing the master) plus `MaxStartups 30:30:100` for cold-start bursts. |
| `ssh_on_gateway()` / `ssh_to_engine()` | Direct SSH to the engine (no hop — it *is* the gateway); the latter is an alias kept for readability. |
| `wait_for_ssh()` / `wait_for_http()` | Poll-based, never raise: warn and continue on timeout. Deploy aborts only when *every* target of a phase is unreachable. |
| `wait_for_boxes_ssh()` | Polls each target through the gateway; Windows is probed via guest agent (no key auth exists). Raises only if **all** boxes fail — that looks systemic, and aborting beats burning through the DNS/auth/nakon retry loops for boxes already known unreachable. |
| `wait_for_cloud_init()` | cloud-init can revert planted perms (sudoers, sshd_config) after a clone, so it must finish before nakon plants anything. Windows is skipped (no cloud-init; `bootstrap_windows_box()` is its equivalent). Exit code 2 means done with non-fatal warnings — still finished, still success. |
| `is_windows_template()` | `"win" in template_name.lower()` — **one** definition, in `windows_ops.py`; `deploy`, `golden_ops`, `nakon_ops` and `ssh_ops` all import it (deduplicated 2026-10-02: there used to be three copies with nothing enforcing that they agreed, and a silent divergence builds the wrong range). |

## range_ops.py (facade) and the modules behind it

`range_ops` re-exports every name it used to define; the implementations live in single-purpose
modules, and **tests patch the owning module** (`patch.object(pve_api, "proxmox_api")`,
`guest_exec.time`, `vm_ownership.has_clone_marker`, `vm_lifecycle.list_snapshots`) because patching
the facade name does not reach the moved code:

| Module | Owns |
|---|---|
| `pve_api.py` | `proxmox_api`/`proxmox_api_for`/`proxmox_request` (TLS pinning), node route table (`register_node_routes`), `cluster_vms_for`, `wait_for_proxmox_task`, `live_vmids`, node-load throttle, `destroy_bridge_if_exists` |
| `guest_exec.py` | `guest_agent_exec_root/windows/detached`, `guest_file_read`, `wait_for_guest_agent`, `diagnose_unreachable_box` |
| `vm_ownership.py` | the ownership proof + every ownership-guarded mutation (below) |
| `vm_lifecycle.py` | `vm_status`/`stop_vm`/`start_vm`, snapshot helpers — no ownership decisions |
| `targets.py` | `vm_id_for`, `box_index`, `enumerate_targets`, `persist_targets`/`load_targets` |
| `terraform_workdir.py` | per-competition terraform dir, `team_vmids_from_state` |

Reads `TF_VAR_proxmox_*` from the environment; no prompts, no phase logic.

**Ownership lives in `vm_ownership.py`, once.** `ownership_verdict(node, vmid, raw_tags, expect_tags)`
returns `owned` (full ownership set present), `marker` (no tags + this competition's clone marker),
`unproven` (no tags, no marker) or `missing` (tags present, set incomplete). `destroy_vm_if_exists`
(refuses unless owned/marker) and the preflight collision gate (ours = owned/marker) both call it,
so teardown and preflight can never disagree about whose VM a vmid holds.

| Symbol | Note |
|---|---|
| API token + `verify=False` | Proxmox API-token auth talks to the API over the same self-signed cert `main.tf`'s provider sets `insecure = true` for: same tradeoff from Python. Each entry point that imports this module calls `urllib3.disable_warnings()`. |
| `proxmox_api()` | Retries transient connection blips (pveproxy drops) up to 4 attempts with a short backoff, on `ConnectionError`/`Timeout` only, so real HTTP failures are not masked. |
| `wait_for_proxmox_task()` (1800 s) | Must tolerate slow storage: clones and deletes can exceed 10 minutes on this host. |
| `vm_id_for()` | The sole Python derivation of `200 + identifier*10 + box_index` (mirrored inline in `main.tf`; changing one without the other collides). |
| `guest_agent_exec_root()` | bash as root via the QEMU guest agent over virtio-serial; no sudo needed, so it bypasses broken sudo (writable-sudoers, ungranted NOPASSWD) and needs no network. Returns `(rc, out, err)`, raises on agent-level failure. |
| `guest_agent_exec_windows()` | PowerShell as SYSTEM via the agent; `-EncodedCommand` (UTF-16LE base64) avoids quoting issues. The only channel into a Windows box before bootstrap. |
| `wait_for_guest_agent()` | Agent ping loop; virtio-serial works with no network. Returns bool, never raises, so callers decide whether absence is fatal. |
| `diagnose_unreachable_box()` | Guest-agent report of the box's IPv4 state and `cloud-init status --long`. Never raises; turns "SSH failed 8×" into an actionable line in the failure handler. |
| `stop_vm()` / `start_vm()` | Graceful ACPI shutdown first for filesystem consistency, forced stop only if ACPI is ignored; `start_vm` is a no-op for a missing or already-running VM. |
| `enumerate_targets()` | One entry per `(team, box)` with vmid/IP/name pre-derived. Invariant: **build from the full lists, then filter — never filter `boxes` and re-enumerate**, because `vmid` is positional in the box list, so a filtered re-enumeration assigns wrong IDs and collides. `vm_name` records what the VM is actually called in Proxmox (`team1-<box>` for the anchor team, `<identifier>-<box>` otherwise) so nothing re-derives it; `machine` is `<box>-team<identifier>`, nakon's opposite-order naming. |
| snapshot semantics | `tz-base` = booted, networked, pre-nakon; `tz-ready` = as-delivered post-nakon+hardening. `tz-base` is taken once for **all** boxes at the end of the phase-4 block (goldens take theirs pre-plant inside the golden build). |
| `take_snapshot()` | Disk-only (`vmstate 0`, fsfreeze via guest agent); deletes an existing same-named snapshot first (replace semantics). **Never raises** — snapshots are optional recovery, and thick LVM cannot snapshot at all. |
| `rollback_snapshot()` | Requires a stop/start cycle (snapshots hold no RAM); raises on failure, unlike `take_snapshot`. |
| `list_snapshots()` | Empty set when the VM is gone or the API refuses: callers treat "no snapshot" and "couldn't ask" the same and fall back to a mode needing none. |
| `snapshot_support_hint()` | The one-line explanation for missing snapshots (range predates snapshotting, or thick LVM), pointing at `--mode reconfigure`/`rebuild`. |
| `destroy_vm_if_exists()` | Stop (await) then delete (await). With `expect_tags` set the cleanup sweep **refuses to destroy any VM whose tags do not contain the FULL ownership set** `tezcatlipoca` + `comp-<name>` + the deploy's `run-<id>` tag (constants.ownership_tags) — a stray VM at a computed vmid is never touched, and neither is a same-comp VM from a DIFFERENT run (two worktrees can deploy the same competition ID; the run tag is what tells their lineages apart). **Untagged VMs are refused** unless they carry this competition's clone marker (`vm_ownership.clone_marker` — an interrupted clone whose tagging step never ran); there is no escape hatch. `retag_ownership()` re-stamps adopted templates (M4 hash reuse across runs) so they stay recognizable. |
| phase-1 cleanup pool | Worker pool of ≤8 over the ownership set: computed team vmids, the engine vmid (deliberately broader than Terraform state — a skipped clone would let phase 5 "resume" onto a stale disk with the previous run's passwords). Every destroy requires the PRIOR run's ownership set (`ctx.reclaim_tags`), so a concurrent same-comp deploy from another worktree is refused, not reclaimed. Deletes are metadata-light on the API; the 596/pvestatd hazard belongs to bulk clone *writes*, which stay serial. Bridges are torn down only when no VM is still attached (`_bridge_in_use` — bridge names collide across same-comp deploys sharing team identifiers). |

**Run ownership (run ids).** Every deploy mints `run-<8 hex>` once per competition directory
(`utils.mint_run_id`, persisted in `.deploy_state.json`, reused by resume/redeploy/crash-loop) and
stamps it as a PVE tag on everything it creates: terraform adds `run_tag` to the engine + team-box
tag lists, golden/jump/engine-template/boot-smoke creation builds it into their ownership strings.
The comp tag alone is shared by every worktree running the same competition ID, so destruction
identity is **the full set** — this is the 2026-10-02 near-miss fix (smoke vs live-2box ran
`same-type-2box-2026-09-29` concurrently; either phase-1 sweep would have eaten the other's VMs).
Teardown refuses state without a run id, and preflight classifies a comp-tagged VM without the
run tag as a collision, never as "ours".

## preflight/ — the single pre-Proxmox gate

`preflight.run_preflight(plan)` (`preflight/gate.py`) is the one entry point deploy calls before it
mutates Proxmox; `config_ops.preflight_gates` / `preflight_gates_multinode` are thin wrappers that
build a plan and call it. A `PreflightPlan` is a list of `NodeShare`s: **single-node is one share
over the whole cluster view, multi-node is one share per hosting node scoped to what that node
holds** — there is no second implementation. Order: (1) `concurrency.py` flock warning (once, both
modes; warns, never refuses — concurrent ranges are supported, the per-resource gates below refuse real collisions); (2) per share: `templates.py` (tagged templates resolve, cloud-init drive, engine base
image, jump clone source), `clashes.py` (vmid + bridge collisions via `ownership_verdict`; the
slot math and labels are `expected_slots`/`expected_bridges`), `headroom.py` (datastore, thin
factor); (3) `mgmt_ip.py` (engine static IP; satellite jump IPs across all nodes); (4) `catalog.py`
(`nakon catalog check`); (5) `pins.py` (advisory only: warns when a managed box carries zero pins — it plants a 0-step plan, a clean no-op since nakon 22360ba). Wording/ordering differences between the modes are confined to the
`NodeShare.multi` flag (message text and report order only). Tests patch the owning module
(`preflight.clashes.proxmox_api`, `preflight.headroom.proxmox_api`, `preflight.templates.proxmox_api`,
`preflight.concurrency.gate_concurrent_deploys`, `preflight.catalog.catalog_gate`).
Redeploy/destroy callers use the same `vm_ownership` proof; call `run_preflight` before any new
Proxmox mutation path.

## config_ops.py

Team identity/secrets and persistence (`collect_teams`, `random_password`, `update_env`,
`write_text_atomic`/`write_state`, `load_packet_passwords`, `load_boxes`). Facade over
`config_prompts.py` (`collect_boxes`, `collect_users_config`, `confirm_deploy`,
`list_proxmox_templates`), `injects_config.py` (`load_injects`, `resolve_inject_times`,
`injects_fingerprint`) and `preflight/`. The symbol notes below keep their old names.

| Symbol | Note |
|---|---|
| `update_env()` | Rewrites `TF_VAR_*` lines in `.env` in place (preserving comments and order) **and** mirrors each value into `os.environ`, because the `terraform` subprocess calls later in the same process read the environment loaded once at import time — before these values were known. The replacement line is passed to `re.subn` as a **function, not a string**: `re.sub` interprets backslashes and group refs (`\1`, `\g<0>`) in a string replacement, which would crash or corrupt `.env` for free-form values (e.g. an `event_name` containing a backslash). |
| `list_proxmox_templates()` | Mirrors `main.tf`'s own lookup (`data.proxmox_virtual_environment_vms.templates` filters on the `template` tag alone) so the picker only offers names Terraform resolves. The engine's own template is excluded — it is looked up by `TF_VAR_template_vm_id` even though it may carry the same tag. |
| `destroy_bridge_if_exists()` | Only HTTP 404 means "no such bridge"; every other error warns (a swallowed failure leaves a stale bridge behind). |
| `_prompt_int()` / `_prompt_difficulty()` / `_prompt_optional_int()` | Re-prompt instead of crash on non-numeric input; blank takes the default, and for `_prompt_optional_int` blank means `None`, which is meaningful for `disk_gb`. |
| `collect_teams()` | `identifier = 100 + i`, so `team1` → `101`. Refuses any identifier whose full 10-slot vmid block overlaps the engine, engine-template, or golden block — the old check covered the engine only, and the collision surfaced hours later at apply #2. |
| `collect_boxes()` | The box set is per-competition, not global: asked fresh at creation, and a reused competition replays its saved `boxes.json`. Template selection accepts an index or an exact name from `list_proxmox_templates()`. **Blank `disk_gb` keeps the template's own disk** (Terraform only emits a disk block when set, because Proxmox cannot shrink — a smaller value fails the clone). |
| `collect_users_config()` | Credlist input must be exactly 3 names, else it falls back to defaults; always returns values so `users.json` can be written for pinning. |
| `load_injects()` / `resolve_inject_times()` | Inject offsets are minutes relative to competition start, resolved to RFC3339 at creation time (phase 7, right before `create_injects`) so they anchor to the actual start, not to deploy start. |
| `load_boxes()` | Reusing a competition replays its saved `boxes.json`, not anything derived from the current `.env`. |

**Inject timing — two consequences.** A `redeploy --mode rollback-ready` reset does **not** reopen
injects: offsets stay anchored at the original phase-7 time (winad-scrim2: every inject closed before
T0). `--reset-event` re-clones the engine from its template (fresh DB) and re-runs phase 7, re-anchoring
at now; `verify` WARNs on any inject already past close. Separately (shakedown-5x4: blue inject score 0),
the scrim path re-anchors at T0 — `scrim.inject_sync.reanchor_injects()` runs unconditionally right after
the event clock starts and pushes via the engine's UpdateInject (`POST /api/injects/{id}`; every existing
`InjectFileNames` entry is re-listed under keep-files, because UpdateInject deletes any attachment not
listed). The heavy `--reset-event` re-clone is no longer required for a rerun.

## nakon_ops.py

**Layout (0.2.0 split).** `nakon_ops.py` is a facade: every public/private name is re-exported, so `from nakon_ops import …` is unchanged. The code lives in `nakon_config_ops.py` (config generation, `os_to_platform`), `nakon_pin_ops.py` (pin/var validation), `nakon_bundle_ops.py` (bundle build + lint), `nakon_lock_ops.py` (engine flock / in-flight detection) and `nakon_run_ops.py` (`run_nakon`). Patch a name on the module whose function *uses* it (e.g. `nakon_run_ops._deploy_owner_check`), not on the facade.

Nakon CLI invocation: config generation, bundle building, and deploy over SSH to the engine. nakon is
a subprocess with `cwd = NAKON_DIR`, never an import.

| Symbol | Note |
|---|---|
| `os_to_platform()` | Classifies a free-text template name exactly as nakon does: `"windows"` if it contains `"win"` (case-insensitive). `redeploy`'s `box_platform()` goes through this so platform classification cannot disagree with nakon's routing. |
| `_nakon_randomize()` | `nakon randomize --platform … --services N --vulns N --exclude … --source auto --json` with `cwd=NAKON_DIR`. On failure it names the real prerequisites: the vulndb (MySQL + vulndb-ui) reachable from this machine, or `VULNDB_UI_URL` set; check `vendor/nakon/.env`. |
| `generate_nakon_config()` | Deterministic re-runs: if either pinned file (`box_services.json`/`box_vulns.json`) exists it is honored, and **both files are always (re)written** so later unconditional reads do not fail on a half-pinned set. Otherwise **one randomization per box type**, so every team gets identical services. Details below. |
| `generate_stage_configs()` | Splits `nakon-config.json` into `.nakon-golden.json` (ONE machine per box type at the golden IP `192.168.<team1 id>.<240+i>`, minus the post-clone subset), `.nakon-repair.json`, `.nakon-final.json` (every team machine, that stage's subset), and the combined `.nakon-postclone.json` view (`repair ∪ final`) that redeploy's convergence sweeps replay. `unbooted` box types (DCs) get NO golden machine — their plants move into the **repair stage** per team (still pre-domain). Machines whose subset is empty are dropped. All four carry `box_password` (same gitignored-secret class as `nakon-config.json`). Multi-node adds `.nakon-golden-slot<N>.json` per slot. |
| `build_nakon_bundle()` | `nakon build --config <abs> --out bundles --json`; content-addressed under `vendor/nakon/bundles/`, cached when the catalog is unchanged; runs with `cwd=NAKON_DIR` for vulndb creds (same error contract as `_nakon_randomize`). Serialized by a module lock: concurrent domain passes (and the golden + post-clone builds) build one at a time operator-side while their deploys run in parallel. |
| `run_nakon()` | scp's the nakon package, bundle, and config to a per-run `/tmp/nakon-<tag>` on the engine, stages into `/opt/nakon/<tag>/`, then runs `nakon deploy` there. Details below. |
| `_run_single_nakon_config()` | Deploys one machine with an overridden configuration list in isolation (own temp config, own bundle, own `--only`), which is reboot-safe; used by the domain pass, where each step reboots the box and would truncate a combined plan. |

**`generate_nakon_config()` budgets and sorting.** **One randomization per box type** is what makes
Quotient's wildcard-IP checks (`192.168._.<octet>`) valid — every team defends the identical set.
Budgets are `ceil(difficulty/3)` services (min 1) and `difficulty` vulns (min 1). **Disruptive vulns sort
last** (entries may be a string or `{"name", "vars"}` — normalized for the sort): they break DNS/apt, so
package installs must land first. A second pass fills `REQUIRED_VARS` identity vars into the base machine
list, because the bundle lint rejects a script referencing a var the base list does not declare even when
every stage file carries it.

**`run_nakon()` staging and concurrency.** **Staging is per-run (M2.3)**: the old shared
`rm -rf /opt/nakon/*` slot was what concurrent runs raced on (the incident behind the engine lock's
deploy-owner check); each run now creates and removes only its own dirs (`finally`), the timeout `pkill`
is scoped to the run's staging path (`pkill -9 -f '/opt/nakon/<tag>'` — the remote cmdline carries it),
and the per-deploy `pip3 install paramiko` became check-then-install so concurrent runs do not race the
engine's pip. The global `/opt/nakon/.deploy-owner` marker remains as a cross-HOST guard (same-host runs
share a holder identity and pass). `only=` scopes to machine names **without changing bundle content**;
`strict=True` passes `--strict` so failures abort instead of reporting live. `jobs > 1` passes nakon's
`--jobs` (default from the `nakon_jobs` Compfile knob, 4): per-machine work is atomic in nakon's runner,
so machines plant concurrently while each machine's step order is preserved. The concurrency ceiling is
engine egress/CPU and mirror throughput, never the datastore (plants do no bulk storage writes). Redeploy
deliberately keeps `jobs=1` — a mid-event repair sweep stays maximally gentle on live boxes.

## engine_ops.py

**Layout (0.2.0 split).** Facade over `engine_cmd_ops.py` (the one `_run_engine_cmd` every remote step uses; every call site must pass `step=`), `engine_bootstrap_ops.py`, `engine_guard_ops.py`, `engine_identity_ops.py`, `engine_health_ops.py` and `engine_event_ops.py`; import paths via `engine_ops` are unchanged.

Scoring-engine bootstrap (Docker, Quotient), NAT/firewall durability, and the `event.conf` push.

| Symbol | Note |
|---|---|
| `bootstrap_scoring_engine()` | `postgres_password`/`redis_password` are generated once per `deploy()` run and passed in so the Quotient stack `.env` written here **agrees** with the one `push_event_conf()` writes later. Clears apt locks first (`killall apt-get apt dpkg`, lock removal, `dpkg --configure -a`) to survive an interrupted first boot — first-boot `unattended-upgrades`/`apt-daily-upgrade` re-spawns apt *after* any single preamble killall, so the units are masked and all three locks polled free (`DPkg::Lock::Timeout` does not cover the archives-cache lock). |
| apt-cacher-ng (M1.3) | Installed and enabled during engine bootstrap; listens on 3142 on all interfaces with zero config. `prep_apt_on_boxes(use_proxy=…)` writes `/etc/apt/apt.conf.d/95tz-proxy` pointing each box at its own gateway IP (`192.168.<id>.1:3142` = the engine's team NIC), gated by the Compfile `apt_cache` knob (default on); `use_proxy=False` actively *removes* the file so toggling leaves no stale proxy. Packages cross the mirror once per competition and the LAN after — that is what lets `nakon_jobs` scale past mirror throttling. Approved trade-off: the proxy is an IP, so apt no longer needs box DNS — `resolv-conf-null-dns` still breaks name resolution for everything else but no longer breaks apt. |
| `docker compose up` (600 s) | The first up after a `--no-cache` build cold-starts postgres (initdb) and recreates every container; measured >60 s twice (e2e-2026-09-19). |
| post-Docker iptables restore | Docker sets the `FORWARD` policy to `DROP` and wipes custom rules on start; forwarding + the team-to-team DROP are re-asserted immediately. The same pass sets `AllowTcpForwarding yes` in `sshd_config` and reloads sshd (the `ProxyCommand -W` tunnels need it). |
| `range-firewall.service` / `.timer` | `After=docker.service`, `OnBootSec=30`, `OnUnitActiveSec=30`: re-asserts team NAT (MASQUERADE) + isolation (DROP) every 30 s for the life of the range, because Docker wipes iptables on any restart. |
| `install_range_healthcheck()` | 60 s timer logging failures only to `/var/log/range-healthcheck.log`. The API probe is deliberately **not** `curl -f`: `/api/login` is POST-only, so a plain GET correctly gets 405 and that still proves the server is up; only curl's `000` counts as down. `OnBootSec=60`/`OnUnitActiveSec=60` is offset from `range-firewall.timer`'s 30 s (`:00`/`:30`) cadence so the two never fire in the same tick. Checks containers running, API responding, isolation DROP present, NAT MASQUERADE present. |
| `ensure_nat_forwarding()` | The engine is every team's NAT gateway (nakon needs apt-get), but Docker resyncs iptables on any container start/restart and drops the team-subnet MASQUERADE, leaving boxes offline — and nakon swallows the resulting apt failures, so services silently do not install. The same resync drops the team-to-team DROP. `range-firewall.timer` re-asserts both every 30 s once live but is not installed until later in `bootstrap_scoring_engine()`, so this covers the gap during phases 3–6. Call it right before any step that needs the boxes online. Only the boxes→internet path needs NAT; engine→box scoring is direct routing on the team bridge. Idempotent by construction (`-C … \|\| -I/-A`). |
| `push_event_conf()` | Takes the same per-run secrets `deploy()` generated: the passwords passed to `bootstrap_scoring_engine()` (so the `.env` rewritten here matches) and the `box_creds` passed to `fix_services_on_boxes()` (which creates those OS accounts). **`box_creds` is what Quotient's Ssh/Smtp/Imap/Sql/Ftp credlist checks authenticate with — the names must equal `fix_services_on_boxes()`' accounts, and there are deliberately no defaults**: any mismatch or stale fallback literal silently scores healthy boxes as down. The credlist lands at `/opt/quotient/config/credlists/linux.credlist`; Quotient is restarted afterward. |
| team bridge NICs | ens19/ens20/… need nothing here — Terraform's `null_resource.team_nics` + `null_resource.reboot_scoring_engine` have fully addressed them by the time this phase runs. |

## windows_ops.py

Windows provisioning over the QEMU guest agent — the only channel before networking or credentials
exist.

| Symbol | Note |
|---|---|
| `bootstrap_windows_box()` | Waits for the guest agent first and **raises on timeout**, because every downstream step depends on it (unlike the polling wait helpers). The embedded PowerShell configures the single `Up` adapter (static IP/prefix 24/gateway/DNS), sets the `Administrator` password via `net user`, enables `sshd` + `QEMU-GA`, and creates the port-22 firewall rule. **The password set here is load-bearing**: nakon's paramiko connection authenticates with it — without this the account keeps whatever the template baked in, which nothing downstream knows. |
| `dns_repoint_windows_box()` | Points Windows DNS at the team's DC before a join: `Add-Computer` needs the SRV records from an authoritative server. |
| `wait_for_windows_sshd()` | Polls `Get-Service sshd` via the agent: agent-up does **not** imply sshd-up after the ADDS reboot. Never raises. |
| `wait_for_dc_dns()` | Polls the DC for `_ldap._tcp.dc._msdcs.<domain>`; DNS may lag the guest agent. Never raises. |

## hardening_ops.py

**Layout (0.2.0 split).** Facade over `box_settle_ops.py`, `apt_dns_ops.py`, `alpine_ops.py`, `service_fixup_ops.py` and `ubuntu_auth_ops.py`; every name (including `_APT_PREP_BODY`, `_SETTLE_CHECK`, `ALPINE_SERVICES`) is still importable from `hardening_ops`.

Post-nakon hardening and DNS/auth fixes over the gateway, with guest-agent fallback.

| Symbol | Note |
|---|---|
| `wait_boxes_settled()` / the old 150 s sleep | The sleep waited out the post-boot unattended-upgrades storm that starves both the guest agent and the ssh forward; the probe polls the actual condition per box instead (agent ping + no `unattended-upgr` process + dpkg lock free via `apt-get check` with a 1 s lock timeout — no psmisc dependency). Ceiling 240 s; a box that never settles falls through to prep's own retry ladder. Runs once per `prep_apt_on_boxes` call (twice per deploy). |
| `prep_apt_on_boxes()` | 5 attempts, 15 s apart, per box (the old blind 150 s sleep is gone); `command -v apt-get \|\| exit 0` first, so a fedora/alpine box is an honest rc=0 no-op instead of 5 retries of `apt-get: command not found` burning a 240 s settle per pass. |
| `fix_dns_on_boxes()` | 8 attempts, 15 s apart, per box; after the last, falls back to `guest_agent_exec_root` with `DNS_FIX_CMD_ROOT`, which needs neither working sudo (NOPASSWD may not be granted yet) nor the network the SSH path uses. **All-fail aborts**: every box failing looks systemic, not a timing fluke. Individual failures warn and continue. |
| `fix_services_on_boxes()` | `box_creds` is required, no fallback: it is the same per-run secret `deploy()` passed to `push_event_conf()`; a fallback literal here would recreate the accounts with passwords Quotient's credlist checks do not know, scoring healthy boxes as down. Per-box script, base64-encoded to avoid all quoting issues. Details below. |
| `setup_ubuntu_auth()` | Enables sshd password auth + NOPASSWD sudo for `box_username` (nakon authenticates by password and runs sudo). 8 attempts, then the same guest-agent root fallback, which beats burning 2 minutes of retries. |

**`fix_services_on_boxes()` — what the script always does.**

- Creates the credlist OS accounts (`useradd` + `chpasswd`) — every auth-based check needs them.
- **Un-wedges sshd.** Several catalog configs (`ssh-root-login`, `ssh-empty-passwords`,
  `ssh-password-auth`, `ssh-max-auth-retries-high`, `ssh-x11-forwarding`, …) each independently run
  `systemctl restart ssh`; landing several back-to-back with no delay trips systemd's crash-loop
  protection (`Result: start-limit-hit`) and leaves sshd down for the rest of the competition —
  confirmed live 2026-09-03 (web01-team101, 5 ssh-\* configs, 5 restarts inside one second).
  `reset-failed` + conditional `start` reaches the box over SSH while it is up and via the guest-agent
  fallback when it is not. This is why the pass runs **after** the repair sweep.
- Per service: MySQL/MariaDB bound to `0.0.0.0` plus credlist DB users (`CREATE USER … @'%'` +
  `GRANT ALL`, first account also `WITH GRANT OPTION`); Postfix `inet_interfaces = all` + re-adding the
  `smtp/inet` master.cf entry; vsftpd anonymous off / local+write enable; nginx restart; **Dovecot**
  plaintext auth on with the 2.4+ key (`auth_allow_cleartext = yes` in a `99-` conf) added **only** on
  Dovecot ≥ 2.4 (version-gated — 2.4 renamed the key), plus a mail dir per credlist account; **bind9**
  `allow-query { any; }` + forwarders, recreating `named.conf.default-zones` if missing and `db.local`
  if the template stripped it; telnet via `update-inetd --enable`; **Splunk**: nakon only creates a
  user, so lighttpd is installed and moved to port 8000 so something listens (Quotient's splunk check
  expects 8000); finally a sweep that starts every installed service.
- When the SSH run exits non-zero — **writable-sudoers-type misconfigs break sudo** — the same script is
  retried via the guest agent as root, with `sudo ` stripped by `re.sub(r"\bsudo ", …)`: `\b`, not
  `^\s*`, so mid-pipeline uses like `echo … | sudo chpasswd` are caught too.

## golden_ops.py

**Layout (0.2.0 split).** `golden_target_ops.py` (slot arithmetic, `golden_targets`, `unbooted_golden_boxes`), `golden_smoke_ops.py` (boot-smoke gate), `golden_disk_ops.py` (disk sizing, root expansion) are re-exported from `golden_ops.py`. `build_golden_set` stays in `golden_ops.py` as the sequencer, with `_build_unbooted_goldens`, `_clone_missing_goldens` and `_convert_goldens` as its extracted steps (the conversion barrier — every golden stopped and smoked before any `POST /template` — is still one function's ordering).

Golden-set build and template conversion (M3.1). One VM per box type is full-cloned from the base
templates to `engine_vmid + 150 + box_idx`, planted with the **golden stage** (strict, tz-base
rollback guard on re-entry), cloud-init-cleaned, stopped, boot-smoked, and converted with
`qm template`. Terraform apply #2 then link-clones every team from those templates.

| Topic | Note |
|---|---|
| why the API, not Terraform | `qm template` conversion happens outside the VM resource's lifecycle, so a Terraform-managed golden would drift the moment it converts. Terraform only consumes the finished templates as `clone { full = false }` sources via the `golden_template_ids` tfvars `deploy()` persists. |
| placement | IPs at `192.168.<team1 id>.<240+i>` on team1's bridge: above the `.1` gateway, below `.255`, and free because golden boxes are converted to stopped templates before any real team box exists. The vmid block is gated by preflight (collisions with team space fail fast pointing at `--scoring-vmid`). |
| resume routing | All golden vmids already templates → skip; some exist as plain VMs → roll the tz-base-marked ones back and re-plant, clone the missing; a *partially converted* set is fatal (templates cannot be un-templated) — destroy and redeploy. M4 exception: partial conversion is fine when every converted vmid's description hash matches its expected hash (selective rebuild / added box type), and only **unconverted slots** are worked on. Cold (unbooted DC) slots: any plain VM is a dead attempt that may have booted and specialized, so it is destroyed and FULL-cloned from the generalized base, then converted **without ever booting**. |
| `unbooted_golden_boxes()` | Absent `domain_roles.json` = no-domain lineup (empty set). A present-but-invalid file (unparseable, non-map, role outside `dc`/`member`, or a name absent from `boxes.json`) raises rather than silently widening the booted set — the silent fallback would hand the DC a booted shared golden, the exact duplicate-DomainSID failure the unbooted split prevents. |
| Windows identity | Linked clones see the same bytes a full clone would; SIDs already duplicate across teams (only the original templates are sysprepped) and stay unique within a team. Live-checked 2026-09-24: phase 6 never re-syspreps. |
| `golden_boot_smoke()` (Compfile `golden_boot_smoke`, default ON) | The invariant "no boot-hostile config rides the golden" used to be enforced by a comment and the hand-maintained `FINAL_STAGE_CONFIGS` set, and it failed for real on 2026-09-24: `systemd-system-masked` planted into a golden disk left **every** linked clone bootless (`strict=True` cannot catch it — the plant exits 0 while the disk is unbootable, and the golden's own running system still answers). After the goldens are stopped and before any conversion, one **throwaway FULL clone per booted golden** is booted and must reach multi-user (guest agent for Windows). Tri-state and fail-closed: `verified-unbootable` and `could-not-verify` both **raise**, so a `POST /template` for that golden never happens. Cold/unbooted DC goldens deliberately skip it (they are converted without ever booting, by design). Setting `golden_boot_smoke 0` skips the gate and prints that the invariant is UNVERIFIED. Honest cost: a full clone + boot per booted box type — PVE only linked-clones templates, and the point is to verify *before* this golden becomes one. Detail: [benchmark-m02.md](benchmark-m02.md). |
| per-box passes run on a bounded pool (4) | The snapshot, password, cloud-init-clean and convert loops were serial; each unit is independent per-box Proxmox work, so they run through `utils.run_concurrent(max_workers=4)` — the same bound `deploy.py` uses for Windows bootstrap and the `tz-base`/`tz-ready` snapshots. Bound 4, not 8: each unit drives a Proxmox task. The `boot_smoke` block is a hard barrier ahead of conversion, and the full-clone loops stay serial (crash-consistent copies saturate the datastore). |
| root-disk expansion | The golden's root fs is expanded guest-side before the plant. A Fedora btrfs root reports `/dev/sda3[/root]`; the bracketed suffix breaks the partition-digit parse and resize2fs cannot grow btrfs, so expansion strips the suffix, grows btrfs natively, and tolerates a late `/dev/dm-N` (`dmsetup mknodes`). Failures retry, then fail hard only when the root fs is measurably too small (regression-4x1 shape — the 15 GB ubuntu template disk filled mid-plant); unmeasurable warns and continues. |

## template_ops.py (M4) — facade over `template_freeze.py` (hashes, freeze/drift gates, git probe) and `engine_template_ops.py` (engine template find/build/destroy)

Per-competition template lifecycle: `build → test runs (reuse; rebuild on config change) → freeze →
competition → destroy`. NOT a cross-competition cache — every template belongs to exactly one
competition, dies at `--full` teardown, and is never patched in place.

| Topic | Note |
|---|---|
| hashes | sha256 per template over only what affects disk contents. Every input is tagged **config-class** or **code-class**; details below. Hashes live on the template **description** (node-side truth) and in `.template-hashes.json` (survives `.deploy_state.json` resets; 0600, gitignored — golden inputs embed `box_password`). |
| engine template | Built API-side like the goldens (terraform only consumes it as `engine_clone_id`); the built template leaves the deployed engine a linked clone with a fresh identity and fresh host keys every run. Details below. |
| reuse vs rebuild | Hash match → reuse (logged); differs + not frozen → destroy that template's clones, destroy the template, rebuild; differs + frozen → freeze semantics; missing → build. Golden granularity is per box type. |
| frozen semantics | A frozen competition never rebuilds: **config-class drift hard-fails naming the changed fields** (the event runs on the hashes that were verified); **code-class drift warns and proceeds off the frozen template** — a post-freeze log line must never break a mid-event team rebuild or engine recovery. Details below. |
| freeze | `verify-competition.py --freeze`: requires all gates PASS including plant coverage, and records hashes + git commit + dirty flag + timestamp + gate results in `.frozen.json` (0600). Windows/domain lineups additionally require `--windows-domain-validated` (the operator's attestation that the run exercised DomainSID uniqueness, machine SIDs, and the three-pass ordering). `--unfreeze --confirm-unfreeze` removes the record — pre-competition use only. |
| teardown modes | `destroy-competition.py` defaults to **teams-only** (clones + engine VM + bridges; templates kept for hash reuse); `--full` also destroys templates (clones strictly first — linked clones die with their base disks); `--full` on a FROZEN competition refuses without `--end-of-competition`. A full teardown also removes `.template-hashes.json` — a surviving record would make the next deploy "reuse" a hash with nothing behind it. |
| team rebuild | redeploy `--mode rebuild` re-clones from the frozen goldens and runs the POST-CLONE STAGES in deploy order (repair → domains → final), never the full config — planting the disruptive/boot-hostile stage before the domain joins is exactly the brick hazard the three-pass split removed. Timed to `.deploy-timings.jsonl`. |
| engine recovery | redeploy `--mode engine-recovery`: `terraform apply -replace` on the engine resource only (with `-target`, so terraform cannot touch `team_box` — without it, a box that drifted from state, which `--mode rebuild` causes by design, gets destroyed mid-event (winad-testrun 2026-09-25)), re-cloned from the engine template, per-deploy state reapplied (fresh empty scoring DB); re-seed with `--from-phase 7`. |
| bundle lint | The catalog DB has no required-vars metadata, so `constants.REQUIRED_VARS` curates the table (`"ip"` = machine identity, auto-filled per machine and banned from the golden stage — a golden-baked identity var would clone the golden's IP into every team; `"literal"` = must be pinned as `{"name", "vars"}`). Every built bundle is linted: payload blobs are scanned for `$VAR` references the step does not declare and the script does not assign/guard/self-default — a catalog config that starts needing an undeclared var fails at bundle build, before any plant time is spent (validated against all 123 existing bundles: zero false positives; the one real hit class is genuinely broken bare pins like `install-package` without `PACKAGE`). |

**Hashes — what is an input.** **config-class**: base template vmid; golden-stage config list including
pinned `{name, vars}` values; the PER-BOX `payload_hash` — that box's golden plan's step `script_sha256`s,
so a web01 pin change does not rebuild app01/dc01/win01 (matrix run 4: one change rebuilt all four); box
user/password; SSH key; apt-proxy flag (engine: base image vmid + pinned `quotient_ref`). **code-class**:
the disk-affecting function sources — engine: bootstrap + clean step — plus the engine's `scoring_engine`
main.tf resource block only, so team-box edits do not rebuild the engine. Excluded, with reasons in the
module docstring: team count/identifiers/IPs, `event.conf`, repair/final-stage configs, snapshot names,
mgmt IPs, DB passwords (all applied after cloning or cleaned from the template). Going 2-team test →
8-team competition rebuilds nothing.

**Engine template build.** Full clone of the base image → bootstrap (pinned `quotient_ref`; the hash
input must be computable BEFORE building, and the realized HEAD is recorded for traceability only) →
clean step (compose down -v, remove `.env`/event.conf/credlists, truncate machine-id, cloud-init clean,
remove SSH host keys; apt-cacher-ng stays installed but its cache is empty at build time — the warm cache
is built on the deployed clone and dies with it) → `qm template`. `.env` goes down BEFORE `compose up` so
the fresh postgres volume initializes with this competition's credentials.

**Frozen semantics.** This holds fully for the ENGINE (the phase-2 gate runs before any engine
destruction), and since 2026-09-28 for the goldens too: the pre-phase-1 `golden_freeze_gate` classifies
each golden's drift and hands the code-only-drifted names to `phase1_destroy_waves` as `frozen_keep`,
keeping the frozen template instead of destroying it (the old behavior destroyed the golden right after
the gate warned "proceeding on the frozen template" — live-found 2026-09-26, scenario-7 leg 2). A
`frozen_keep` golden whose template is MISSING is still rebuilt (nothing frozen to proceed on). Config
drift still hard-refuses before anything is destroyed. Mid-event rebuilds and recoveries warn (not block)
when the node's template hash differs from the verified record.

## domain_ops.py

Per-team AD forests: DC promotion and member joins. Driven by `domain_roles.json`; no-op when absent.
The **sequencing** (why the ADDS reboot needs its own single-machine pass, why the domain chain runs
before the final stage, DSRM/SRV waits) is canonical in
[architecture.md](architecture.md#windows-path) and [deploy_lib](#deploypy--deploy_lib); the internals-only notes:

| Topic | Note |
|---|---|
| `_probe_joined()` | Live membership probe via the guest agent ("root truth", no SSH needed): `Win32_ComputerSystem.PartOfDomain` on Windows, `realm list` on Linux. **Joins are not idempotent** (`realm join`/`Add-Computer` on an already-joined member fails, and the single-config pass runs strict), so resumes must skip members already in the domain. |
| ADDS resume artifact | `.nakon-domain-<team>-adds.json` — its existence means ADDS already ran for this team (re-running `Install-ADDSForest` on a live DC just fails). Promotion is skipped on resume; the joins still run (they are the recoverable part). `.nakon-domain-<team>-ad-misconfigs.json` and `-ad-accounts.json` gate phase-6 replays the same way. |
| AD misconfigs use `strict=False` | This pass is scoring flavor, not range infrastructure: "Disable System Firewall" sweeps every AD computer over WinRM and a Linux realmd member has no WinRM, so the step exits 1 on any mixed Windows/Linux domain after landing its own misconfig. Failures print in nakon's summary instead of aborting. |
| swallowed chain failures | `deploy_domain_configs` aggregates `run_concurrent` results and fails the phase rather than discarding them; a team chain that raised right after promotion used to leave the AD misconfig pass, packet accounts, and member joins silently unplantable (only an hour-later verify FAIL revealed it). `--from-phase 6` re-enters safely (promotion probes + markers). |
| Linux member joins | Join failures are scenario flavor, not infrastructure — the box keeps local auth and every scored service. The realmd apt install can blow past nakon's per-step timeout on small VMs: log loudly and keep deploying (hence `strict=False` + catch). |

## timing.py

Deploy timing instrumentation (M0.1). `timed(comp_dir, phase, op, target)` appends one JSONL line per
operation to `competitions/<id>/.deploy-timings.jsonl` (gitignored, no secrets — op names and
durations only). Phases wrap their big-ticket ops (terraform apply, engine prep, per-VM clone/start/
destroy/snapshot, waits, apt prep, nakon passes with machine counts, ADDS/joins, seed) in `deploy_lib/phases/`
and `domain_ops.py`. Appends are lock-serialized, so M2's concurrent workers can write the same file
safely. `print_timing_summary` totals seconds per (phase, op) at deploy end. Baselines come from real
deploys (see [benchmark-m02.md](benchmark-m02.md), [benchmark-m04.md](benchmark-m04.md)), not
synthetic ones.

## firewall_ops.py

The in-path firewall path (pipeline v3). A box with `"unmanaged": true, "in_path": true` in
`boxes.json` is the team's gateway; everything here keys off `utils.is_in_path_fw()`.

| Topic | Note |
|---|---|
| `generate_team_config` | Pure string surgery on the comp's `pfsense/pfsense-config-orig.xml` seed (promoted from the per-comp `gen_pfsense_config.py` copies — there is deliberately **no pipeline-shipped seed**: the surgery anchors on the factory config's exact stanzas, and a fabricated seed would fail silently mid-deploy). WAN `172.31.<id>.2/30`, LAN `192.168.<id>.1/24`, SSH on, outbound NAT off (the engine keeps the MASQUERADE — box→internet is box → fw → engine → NAT), WAN pass rule any→`lan`. The rule destination must be the `lan` KEYWORD — `<network>` takes a keyword/alias, not a CIDR; a raw CIDR silently drops the rule (pfsense-ad 2026-09-28). `Compfile firewall_dnat PORT->TARGET[,…]` adds LAN port-forwards (the beacon-C2 path once the firewall, not the engine, owns the gateway); `{tid}` in a target becomes the team identifier. Seeds not already carrying it also get the `<system><afterbootupshellcmd>` agent boot hook (see `qemu-guest-agent` row). |
| `bootstrap_firewalls` | No console. Firewalls are clones of the **`pfsense-provision`** template (vmid 958 on .150 since 2026-10-08; built once by `tools/build-pfsense-provision-template.py`): SSH on, the deploy public key in admin's `authorizedkeys`, LAN `vtnet1` = `192.168.1.1/24`, serial console enabled, qemu-guest-agent sealed in. Per team, one at a time (every template boots as `192.168.1.1` on its own isolated bridge, and the engine can hold only one borrowed route): the engine borrows `192.168.1.2/24` on that team's NIC (`ens19+` in sorted team-key order), waits for SSH on `192.168.1.1`, `ssh admin@` through the engine and replaces `/cf/conf/config.xml` with the generated config (`cmp` first — an identical config is left alone, **no reboot**), clears `/tmp/config.cache`, reboots detached, releases the borrowed address in a `finally`, and waits for SSH on the WAN `172.31.<id>.2`. A firewall already answering on its WAN address (a re-run after the cutover) is pushed to there instead — the generated config carries the same key (`TF_VAR_ssh_public_key`). The `node=` argument (phase 5 passes `ctx.node`) enables the agent layer: after the post-push reboot the guest-agent ping is logged as the config-applied boot confirmation, and both failure raises append `fw_agent_diagnostic()` readback (guest interfaces, whether the config landed). Agentless clones (pre-retrofit templates) are normal — the probes stay silent and SSH remains the only gate. |
| `fw_agent_diagnostic` / `_agent_answers` | The agent layer of the bootstrap. `/agent/ping` rides virtio-serial — it answers before any guest network exists, so it distinguishes "QEMU process up" from "the guest actually rebooted" (the `qm status` uptime counter cannot, fw-live 2026-10-04). Both helpers are best-effort: a dead or absent agent returns `False`/`''` and must never become the failure — the SSH probes stay load-bearing. |
| `qemu-guest-agent` (sealed into the template) | Installed offline 2026-10-08 by the builder's `agent-clone`/`agent-install`/`agent-seal` subcommands (full-clone the template → install → verify → reseal under the canonical name; the previous template is renamed `pfsense-provision-agentless-bak`). Facts worth keeping: **pfSense never runs `/usr/local/etc/rc.d/*` at boot** — `/etc/rc`'s local-script pass globs `*.sh` only (`find_local_scripts_new`), so the package's rc script plus `qemu_guest_agent_enable` in rc.conf.local is inert; the only hook that fires is the **`<system><afterbootupshellcmd>` tag in config.xml**, which `/etc/rc.bootup` mwexec's at the end of bootup (`service qemu-guest-agent start`). The tag must ride every config the firewall runs — the template's AND the generated per-team ones (`generate_team_config` injects it). Packages come from `pkg.freebsd.org/FreeBSD:14:amd64` matched against the guest's own `pkg config abi`; `pkg add` needs `IGNORE_OSVERSION=yes` (pfSense's 14-CURRENT kernel reports an older `__FreeBSD_version` than the packages) and the dependency closure staged by hand — the range has no DNS/internet, so ANY future pfSense package addition needs the same offline staging. qemu-ga's binary is `qemu-ga` (not `qemu-guest-agent`) and its flags go in `/etc/rc.conf.local` with `-p /dev/vtcon/org.qemu.guest_agent.0`. `qm guest ping` does not exist on this PVE — the REST `POST /agent/ping` is the verify. |
| `cut_over_engine` / `cutover_netplan_yaml` | Rewrites `/etc/netplan/60-team-ifaces.yaml`: team NICs stay up with NO address (`dhcp4: false` + `optional: true` — an empty netplan entry defaults to dhcp4 and would hang), transit NICs take `172.31.<id>.1/30` and the `192.168.<id>.0/24 via 172.31.<id>.2` routes, `netplan apply`, no reboot (the NICs exist since apply #1). NIC naming mirrors `team_nics` exactly: ens19+ team NICs in sorted team-key order, transit NICs continue the positional sequence. **A later `terraform apply` re-writes the pre-cutover file** — the trigger only fires on real changes, but re-running apply on a live firewall range re-adds `192.168.<id>.1` under the firewall's feet (known-issues). |
| `verify_in_path` | Fail-loud post-cutover gate (routing_ops style): WAN SSH probe, `ip route get` must show `via 172.31.<id>.2`, and the first managed box must be reachable through the firewall — the exact path every later plant/scoring step takes. |

## deploy.py + deploy_lib/

`deploy.py` is a 26-line shim (loads `.env`, re-exports `main`/`deploy`/`prepare`/`DeployContext`);
the pipeline is the `deploy_lib` package, one responsibility per module, with imports flowing one
way (`cli` -> `runner` -> `prepare` -> the step modules; `phases/` and `context` depend only on
leaf helpers, never on `runner`):

| Module | Responsibility |
|---|---|
| `cli.py` | `build_parser` (all flags), `select_named_competition` / `select_interactive_competition`, `write_new_competition`, `print_plan` (`--plan-only`), `main` |
| `runner.py` | `deploy()`: take the lock, `prepare()`, `wait_out_contention` (`--min-load-free`), `run_pipeline` (walk `phases.PHASES`, checkpoint, hook `connect_terraform` after phase 2), `report_failure` |
| `prepare.py` | `prepare()`: the ordered pre-phase steps, then `assemble_deploy_context` flattens the stage records onto `DeployContext` |
| `gates.py` | **Every refusal made before anything is built or destroyed**: `acquire_deploy_lock`, `check_resume_gates` (missing state, pipeline version, `guard_resume_from_phase`, `guard_resume_streak`, `guard_resume_existence`), `run_range_gates` = `check_stale_terraform_state` + `run_capacity_preflight`, and the checkpoint gate (`CHECKPOINT_GATES`). Called from exactly two spots in `prepare()`, both before the secret mint |
| `context.py` | `DeployContext` (what the phases share), `save_state`, `checkpoint` (runs the existence gate before stamping `last_phase`), the ownership-tag properties |
| `stages.py` | The per-step records (`RunIdentity`, `CompetitionSpec`, `PriorDeployState`, `CompetitionInputs`, `CompetitionSecrets`, `EnginePlacement`, `GeneratedConfigs`, `TerraformInputs`, `DeployTargets`) |
| `inputs.py` | Disk reads only: `resolve_engine_vmid`, `load_competition_spec`, `load_prior_deploy_state`, `load_competition_inputs` |
| `secrets.py` | `resolve_competition_teams` (run id + roster, carried secrets on resume), `mint_competition_secrets`, `carry_box_password` |
| `placement.py` | `parse_team_node`, `apply_engine_placement` (resolve placement, activate it, take the endpoint-keyed engine lock) |
| `configs.py` | `generate_stage_configs_and_hashes`: nakon/stage configs, golden hashes, the pre-phase-1 frozen gate |
| `tfinputs.py` | `engine_mgmt_ip_from_env`, `build_terraform_inputs` (`terraform.tfvars.json`, `teams.json`, env copies) |
| `targets.py` | `enumerate_deploy_targets` (managed/linux/windows work lists) |
| `golden_plan.py` | `golden_hash_entries`, `phase1_destroy_waves` (phase 1's teardown decision), `golden_rebuild_gate` (phase 4's per-slot rebuild gate) — both destroy decisions in one file |
| `coverage.py` | `record_coverage` / `record_stage_coverage` / `record_clean_coverage` (plant-coverage bookkeeping) |
| `failure.py` | `failure_signature` / `record_failure` / `clear_failure_streak` (resume budget) and `record_degradations` |
| `phases/` | One module per phase group: `cleanup` (1), `engine` (2-3), `golden` (4), `firewall` (5), `postclone` (6), `final` (7), `seed` (8), `finish` (`connect_terraform`, `finish_deploy`); `_snapshots` and `_terraform` hold the shared helpers; `PHASES` lives in `phases/__init__.py` |

The phase list is canonical in [architecture.md](architecture.md#eight-phase-deploy). Tests patch
through `tests/_deploy_patch.dpatch(name)`, which replaces a name in every `deploy_lib` module
that binds it. The internals-specific notes:

| Topic | Note |
|---|---|
| `prepare()` | Everything before phase 1, as one testable step. Contract (its docstring): resolve the engine vmid, load `Compfile`/config/state, mint or reuse the run's secrets, compute multi-node placement, generate the nakon + stage configs, compute the golden hashes and run the frozen gate, assemble `terraform.tfvars.json`, run preflight, and enumerate the targets. Returns `None` when the operator declines confirmation, so `deploy()` returns without running any phase. The `.deploy.lock` is taken by `deploy()`, not here, and nothing in `prepare()` destroys anything. Safety order: `gates.check_resume_gates` right after the prior state is read, `gates.run_range_gates` (stale-terraform-state refusal, then the capacity/collision preflight) after placement — both before the secrets mint, so a refusal never leaves a credential-desync corpse. |
| `ctx.save_state()` / `checkpoint(n)` | `save_state` delegates to `config_ops.write_state` (atomic `os.replace` + 0600-at-creation) — the single writer for `.deploy_state.json`, which holds the only copy of the box passwords. `checkpoint(n)` records `last_phase = n`, the resume guard's only source of truth. |
| resume semantics (`from_phase > 1`) | Skips the destructive [1/8] cleanup and [2/8] terraform apply, and reloads teams + per-run secrets from `.deploy_state.json` so the resume agrees with what was already deployed. After each *executed* numbered phase the last-completed number is checkpointed (a resumed-past phase is called for its banner but never checkpointed); on failure the exact resume command is printed. `--from-phase N` with **no** state file is a hard `SystemExit` (without it, the fresh-deploy branch minted NEW passwords and overwrote `teams.json` while phases 1–2 were skipped, desyncing the range from its credentials). A state file with a different `pipeline_version` also refuses — the phase numbers mean different things. And `gates.guard_resume_from_phase()` refuses `--from-phase N` when `N > last_phase + 1`: the skipped phases are what build the machines the later ones target, so a resume there buries the real failure in downstream errors. `--force-from-phase` is the deliberate override, for a checkpoint known to be stale (e.g. the process died after a phase finished but before its checkpoint landed). A missing/unusable `last_phase` reads as `0` — the conservative choice. |
| secret-regeneration persistence | When resuming with an older state file that predates a secret field, the missing secret is regenerated once and immediately written back, so a *second* resume reuses the same value instead of minting yet another that disagrees with the engine/boxes. |
| per-competition passwords | `admin`/`inject`/`postgres`/`redis`/`box_password`/`box_creds` are generated fresh per run, replacing the fixed `ubuntu/ubuntu` + `admin/changeme123` literals this once shipped: a fixed value across every deployment is guessable from this public repo or by fingerprinting a past deploy. `box_password` is a golden-hash INPUT (baked into `/etc/shadow` + cloud-init on the golden disk), so a fresh deploy **carries it forward** from prior state, and packet `passwords.json` outranks both — a fresh deploy that re-minted it would rebuild every golden. Postgres/Redis are generated once and passed to both `bootstrap_scoring_engine()` and `push_event_conf()` so the two `.env` writes agree. |
| `KNOWN_BROKEN_TEMPLATES` | Warn-don't-block: `ubuntu24.04` and `debian13-lite` have bad cloud-init; clones never get a working network/SSH (use the `-fix` variants). The warning is unconditional — not gated by `--yes`/`confirm_deploy` — because a reused competition replays its saved `boxes.json` exactly and can outlive the interactive box-picker warning, and a non-interactive deploy skips that prompt anyway. |
| stage bundles | Built operator-side — the only steps that need the vulndb (MySQL + vulndb-ui/MinIO), which `generate_nakon_config()` has already proven reachable. The pipeline builds three stage bundles (golden + repair + final), not one full bundle, so **the scoring engine never sees vulndb credentials** and no full-machine pass is ever deployed. |
| `all_targets` | Built once from the FULL box list — see `enumerate_targets()`'s invariant; nothing downstream may re-derive a vmid from a filtered `boxes`. |
| `teams.json` | Persisted because `destroy-competition.py` requires it to tear down later; verification also reads team logins from it instead of re-deriving. |
| `confirm_deploy()` / sweep marker | `confirm_deploy()` is skipped on resume (resuming implies prior confirmation); `--yes` skips it on a fresh deploy. A fresh deploy unlinks `.postclone-swept` so it never inherits a previous deploy's marker, and apply #2 unlinks it again after (re-)creating the team boxes so a phase-5 resume always re-sweeps the fresh clones (the old workaround was deleting the marker by hand; hit twice on shakedown-5x4). |
| [1/8] teardown | Two waves per hosting node (team boxes, then engine + goldens that are missing, hash-mismatched, or a stale slot beyond the lineup). Linked clones must die before their templates. `reset_domain_markers()` clears `.nakon-domain-*` so a redeploy does not skip a promotion because a stale marker said it was done. Bridges serial. |
| [2/8] apply #1 | `-parallelism=1` with `build_team_boxes=false`: the engine (a **linked clone of the engine template**) + all team bridges + the engine's team NICs (netplan) + the cold boot. `parallelism=1` because concurrent **full** clones saturate the datastore/API (HTTP 596); linked clones are metadata work, but the setting stays 1. The engine's SSH reachability is **polled** (`wait_for_ssh`) instead of a blind post-apply sleep, and `forget_engine_host_key` drops any stale pin (a fresh engine VM means a fresh host key). `state["deployed_endpoint"]` is recorded here for the stale-state guard. |
| [3/8] prepare engine | Per-deploy state applied fresh (`.env` before compose up, fresh volumes, apt-cacher check), `event.conf` pushed early, NAT re-asserted. Pushing `event.conf` before any nakon run prevents the Quotient crash loop that wipes NAT. |
| [4/8] golden + apply #2 | See `golden_ops` (incl. the phase-barrier boot smoke before conversion) and `template_ops`; then Windows bootstrap for ALL teams (guest agent is the only pre-network channel), concurrent waits, `setup_ubuntu_auth`, DNS fix, apt prep, and `tz-base` for every box. Clones boot with working DNS/apt because the golden disk carries no disruptive configs. |
| [5/8] firewall bootstrap | Only when the lineup has an `in_path` box (print-and-return otherwise, keeping the phase index stable). Order is load-bearing: the engine still owns the team bridges' addressing while the configs are pushed, and phases 6-7 must not run before the cutover or their engine→box work would target the wrong path. The SSH push (`firewall_ops.bootstrap_firewalls`) is functional end to end — SSH answering on the WAN address is the success signal. The cutover rewrites `/etc/netplan/60-team-ifaces.yaml` — **a later `terraform apply` rewrites that file back to the pre-cutover shape** (trigger-gated, so only on a real trigger change): re-running apply on a live firewall range re-adds `192.168.<id>.1` under the firewall's feet. The phase writes its completion to `state["firewalls_bootstrapped"]` for reporting; resume re-pushes (an unchanged config is not re-applied). Firewall `tz-base` snapshots are taken HERE, after the cutover, not in phase 4 — see architecture.md. |
| [6/8] repair sweep | `.nakon-repair.json`, lenient (`strict=False`: one flaky plant must not kill the sweep after 98% landed), `--jobs N`, gated by `.postclone-swept`. `fix_services_on_boxes` runs **after** the sweep: the ssh-\* configs restart sshd and can trip the start-limit, so the un-wedge + credlist accounts + service binds belong after the thing that breaks them. (Historical note: through 2026-09-24 the equivalent sweep passed no `only=` at all — the monolith's comment said "team1 already done" but the filter was never implemented, so team1 was re-planted over its finished strict plant on every deploy.) |
| [7/8] domains → final → beacons → assume-breach | `deploy_domain_configs` runs teams **concurrently** (each team's ADDS→join chain serial within itself); the final pass runs after it (disruptive + boot-hostile); team beacons (if `team_beacons 1`) are planted **before** the `tz-ready` snapshot so restore points carry them. Assume-breach red presence (if `assume_breach 1`, `red_plant_ops.py`) deploys bad-auto's red01 + the realm engine DNAT and runs the day-0 seed — access + prebaked Realm C2 beacons + the persistence/evasion layer — also **before** the snapshot, so the boxes are already compromised at T0. It records red01's identity in `state["assume_breach"]`; **teardown must run `badauto destroy` for red01** (destroy-competition's run tags never covered it). `state["nakon_failed_steps"]` is **merged**, not overwritten: a failure in both passes must keep the repair tally, duplicates dropped, capped at 40. |
| [8/8] seed | Quotient's HTTP is polled (`wait_for_http` on `/api/login`) instead of a blind sleep. Each sub-step is gated on its own state flag (`seeded`, `engine_unpaused`, `injects_created`) because **`unpause_engine` is not idempotent** — a resume in the crash window between POST and flag-save asks the engine first (`engine_paused`) and re-POSTs only when it really is still paused. `resolve_inject_times()` runs here so offsets anchor to the actual competition start. |
| failure handler | `current_phase` is tracked so the handler names where to resume. An "already exists" error at phase ≥ 2 suggests a Proxmox/Terraform state mismatch, so the recommended resume point is **phase 1** — resuming from the failed phase would hit the same error again. |
| `credentials.txt` (0600) | The durable, non-log record of every secret this run generated; the console prints them for convenience but the file is authoritative. Includes `box-login` and `box-credlist-*` lines. |
| `--plan-only` | Collects/generates the config and prints a summary, then exits without touching infrastructure. Exists because there is otherwise no confirmation checkpoint between the box picker and a real deploy: `--yes` skips it and piped/scripted stdin that satisfies every remaining prompt walks straight into a real deploy. |
| `cli.main()` paths | `--competition` reuses an existing competition straight to `deploy()` or creates one, falling back to prompting only for missing pieces; boxes are still collected interactively (no non-interactive box spec yet). The interactive path passes `--teams`/`--yes`/`--from-phase` through with None/False/1 defaults. |

## pipeline_api.py

The stable import surface the hyphenated entry-point scripts cannot be: `create-competition.py` and
its companions are not importable under their own names (the dash is not valid in a module name),
and the old workaround loaded `create-competition.py` through `importlib` and reached whatever its
shim happened to have imported as `driver.<symbol>`. That hid `redeploy-competition.py`'s true
dependency set — the shim re-exported all 74 names it imported for its own use, so a consumer could
start using a new symbol unnoticed, and the shim's surface could change silently under it.

- **Explicit `__all__`** — 17 names, each imported from the module that owns it (`nakon_ops`,
  `golden_ops`, `hardening_ops`, `ssh_ops`, `domain_ops`, `engine_ops`, `windows_ops`,
  `config_ops`, `constants`). No re-export chains. `__all__` is also what stops pyflakes reporting
  every name as unused.
- **Growth rule** — add a name only when a consumer needs it. An unused re-export is invisible
  again by construction, which is the bug this module removed (`tests/test_pipeline_api.py` pins it).
- **`create-competition.py`** — now a thin CLI: load `.env` (before importing `deploy`, which reads
  `TF_VAR_*` at import time), `import deploy`, `deploy.main()`. `deploy.py` loads the same `.env` at
  its own module scope; `load_dotenv` does not overwrite already-set variables, so the second load
  is a no-op.

## create-competition.py

Thin CLI entry point only — see [pipeline_api.py](#pipeline_apipy) for the library surface and why the
hyphenated script cannot be imported directly. `ENV_PATH`/`load_dotenv` live in `deploy.py`; there
is no compatibility re-export layer anymore.

## destroy-competition.py

**Layout (0.2.0 split).** The script is the CLI + sequencer. `destroy_gate_ops.py` holds every refusal that must fire before anything is touched (`refuse_frozen_full_teardown`, `load_ownership` = the run-id anchor + endpoint guard, state read once); `destroy_sweep_ops.py` the mechanics (`pre_stop_windows_boxes`, `destroy_with_recovery`, `sweep_tagged_leftovers`, `report_remaining`); `destroy_templates_ops.py` the `--full` golden/jump/engine-template teardown. Tests patch the sweep helpers on `destroy_sweep_ops`, and `main()`'s own collaborators (`destroy_with_recovery`, `pre_stop_windows_boxes`, `artifacts_ops`) on the script.

Teardown: team boxes first, then `terraform destroy`, then goldens on `--full`.

| Topic | Note |
|---|---|
| TF_VAR restoration | Per-competition `TF_VAR_teams`/`TF_VAR_boxes_per_team`/`TF_VAR_event_name` are restored from the saved files so `terraform destroy` uses the **exact same resource keys as the original apply**: `for_each` over `var.teams`/`var.boxes_per_team` must match, or resources are orphaned. It also restores the comp's endpoint/node/vmid, mirroring the deploy's stale-state guard (a destroy against a different host makes terraform reconcile foreign resources — live-found 2026-09-25). |
| run-id ownership | `.deploy_state.json`'s `run_id` anchors the whole teardown: `sweep_tagged_leftovers` requires the full set incl. the run tag (`main()` refuses to run at all when the state carries no run id); `pre_stop_windows_boxes` stops only name-matching VMs that ALSO carry the ownership tags; `report_remaining` classifies every survivor as OURS / same-comp-different-run / no-run-tag; the `--full` golden/engine/jump destroys pass the run-tagged ownership set. |
| golden wave | After `terraform destroy` removes every team box, engine, and bridge, `destroy_golden_set` API-destroys the golden templates (`engine_vmid + 150 + i`) with the ownership-tag check (`tezcatlipoca`, `tezcatlipoca-golden`, `comp-<name>`, `run-<id>`). Clones must die before their templates' base disks. |
| `-parallelism=4` | Deliberately higher than the applies' `-parallelism=1`: deletes are metadata-light (the 596/datastore hazard belongs to bulk clone writes). |
| `load_destroyable_competitions()` | Requires `Compfile` + `teams.json` + `boxes.json`: competitions deployed before `teams.json` support cannot be safely destroyed this way. |
| hung Windows DC | bpg shuts down gracefully (long `timeout_shutdown_vm`) and a DC whose guest agent is down never shuts down, so `terraform destroy` hangs. Hard-kill the qemu process (`kill -9 $(cat /var/run/qemu-server/<vmid>.pid)`), or `qm stop` the other DCs first. |

## redeploy-competition.py

**Layout (0.2.0 split).** The script is the CLI + dispatch. `redeploy_gate_ops.py` loads the range and enforces the `pipeline_version`/`run_id` gate before any Proxmox call; `redeploy_select_ops.py` (selectors, `box_platform`), `redeploy_plant_ops.py` (`run_nakon_and_harden`, `rerun_domain_configs`, `prepare_nakon_assets`), `redeploy_light_ops.py` (`mode_resync`/`mode_rollback`/`mode_reconfigure`), `redeploy_rebuild_ops.py` (`mode_rebuild`, `template_vmid_for`), `redeploy_reset_ops.py` (ladder + health probe) and `redeploy_engine_ops.py` (`engine_recovery`, `reseed_event`). Patch a collaborator on the module whose function calls it. `quote_sshkeys` lives in `ssh_ops`.

Filtered per-team/box rollback/reconfigure/rebuild against a live range, using snapshots. The mode
table (incl. `resync` and `engine-recovery`) is in
[usage-agents.md](usage-agents.md#redeploy-modes-cheapest-first); the internals notes are:

| Topic | Note |
|---|---|
| post-clone stage | Repair re-plants (`rollback-base`/`reconfigure`/`rebuild`) run the **post-clone stage** (`.nakon-postclone.json`), not the full config: the golden-stage installs ride the linked clone, and re-running them over live boxes mid-event is exactly what the stage split removed. |
| `rebuild` | Clones from the golden template (`state["golden_template_ids"]`, linked) so a rebuilt box carries the golden-stage installs, and refuses with an explanation if the golden is gone. It deliberately does **not** clone the anchor team's live box (its defenders have changed it). A rebuilt anchor-team box was recreated outside Terraform, so the next `terraform apply` sees drift and wants to replace it — fine mid-event; re-import or accept the replacement afterwards. |
| `resync` | Cannot recover `box_password` — it is baked into the boxes at bootstrap and lives nowhere on the engine — so it aligns everything else and re-sets the credlist accounts plus the box login it knows, reporting any box the guest agent could not reach. |
| domain chain on rollback | `rollback-base`/`rebuild` re-run `deploy_domain_configs()` for any selected box with a role in `domain_roles.json` (restoring a pre-nakon disk undoes ADDS/joins), deleting the stale ADDS done-marker first. **If the domain chain cannot run, `tz-ready` is deliberately NOT re-taken** — snapshotting then would bake a broken state in as "as delivered". `reconfigure` never resets disks, so domain membership is assumed intact. |
| `box_platform()` | Routes through `pipeline_api.os_to_platform` (the single map nakon also uses), so platform classification cannot disagree with nakon's routing. Replaced the old `_load_driver()`/importlib access to `create-competition.py` (see [pipeline_api.py](#pipeline_apipy)). |
| secrets must be the originals | `box_password`/`box_creds` come from `.deploy_state.json`: the credlist accounts this recreates must match what `push_event_conf()` wrote to Quotient's `linux.credlist`, or a recovered box scores down on every auth-based check while healthy. |
| symbol helpers | `template_vmid_for()` resolves a template name to a vmid exactly as `main.tf`'s templates data source does (tagged `template`, excluding the scoring template). `quote_sshkeys()` — Proxmox's `sshkeys` config param wants the key URL-encoded. |
| Linux-only executors | `fix_dns_on_boxes`/`setup_ubuntu_auth`/`fix_services_on_boxes` (bash against Ubuntu boxes) receive Linux targets only on every path; Windows keeps the guest-agent bootstrap, the domain chain, waits, snapshots, and nakon. |
| snapshot precheck | Rollback modes fail fast (with `snapshot_support_hint`) when a selected box lacks the required snapshot, before touching anything. Every mode except `reconfigure`/`resync` discards the defenders' work and says so before asking. |

## verify-competition.py

Post-deploy verifier: logins, services, isolation, in-path firewall (`verifier/firewall.py`), misconfig spot-check, injects, pins, plant coverage,
domains, red identity, packet. Every gate returns a `GateResult` carrying the module-level `Status`
(`PASS` / `FAIL` / `SKIP_UNAVAILABLE`), and the SUMMARY and the exit code are both derived from the
same list — the printed word and the verdict can no longer disagree. The gate list a reader cares
about is in [usage-agents.md](usage-agents.md#verify-competitionpy); internals notes:

**Layout.** `verify-competition.py` is a thin entrypoint (it calls `verifier.cli.run()` and
re-exports the gate API under `__all__` for the tests and `test_deploy_phase_units.py`, which
load it by path — a hyphenated filename cannot be imported). The code is the `verifier/` package,
one concern per module; gates call each other's hops through the defining module
(`context.ssh_via_gateway`, `scoreboard.check_services`, …), so a test patches exactly one place
(`patch.object(verifier.context, "ssh_via_gateway", …)`), never the entrypoint.

| Module | Responsibility |
|---|---|
| `verifier/cli.py` | `build_parser()` (every flag) + `main()`: placement activation, ctx/teams/boxes load, `--unfreeze`, run, SUMMARY, `--freeze`, exit code; `run()` adds the Ctrl-C exit 130 |
| `verifier/runner.py` | `run_gates()` — the ordered gate sequence and the `--timeout` guard between gates (`_Run.spent`); returns a `RunOutcome` (results + the plant-coverage result `--freeze` needs) |
| `verifier/model.py` | `Status`, `GateResult`, `CheckError`, and the one set of constructors every gate uses (`gate_pass`/`gate_fail`/`gate_skip`/`bool_gate`) |
| `verifier/verdict.py` · `summary.py` | `RunBudget`, `gate_verdict` (exit code + freeze dict), `summary_lines` · the printed SUMMARY block (tally, template hashes, freeze marker) |
| `verifier/context.py` | terraform `agent_context`, `--engine-ip` override, ssh key resolution, `ssh_via_gateway` / `ssh_to_engine` / `red_ssh_argv` (one shared ssh option tuple), `REPO_ROOT` |
| `verifier/loaders.py` · `boxes.py` | comp-dir readers (`load_teams`, `load_boxes`, `read_credentials_lines`, `read_deploy_state`, `count_local_injects`) · machine-list predicates (`is_linux_box`, `boxes_by_team`; the "win" rule is `windows_ops.is_windows_template`) |
| `verifier/creds.py` · `packet.py` | admin-password lookup + default-credential guard · `--packet` credential and out-of-scope-account gates |
| `verifier/scoreboard.py` · `engine.py` | logins + services (freshness, pins) · injects + round loop |
| `verifier/isolation.py` · `red.py` | FORWARD-DROP rule + live cross-team probe · `--red-identity` / `--red-teams all` |
| `verifier/misconfig.py` · `reports.py` | `MISCONFIG_CHECKS`, spot-check (SSH then guest-agent fallback), clone survival · informational healthcheck/beacon reports |
| `verifier/domains.py` · `state_gates.py` · `freeze.py` | AD domain gate · `.deploy_state.json`-fed gates (plant coverage, degradations) · `.frozen.json` write/remove |

| Topic | Note |
|---|---|
| tri-state gate model | `SKIP_UNAVAILABLE` means the gate could not be evaluated (dead SSH, missing state, no vantage point) and is **non-passing** when the result gates: a check that never ran exiting 0 is how a dead box reads as a healthy range (live-found 2026-10-02: isolation's cross-team probe "PASSed" on stopped VMs). A `gating=False` result is reported in the SUMMARY as `[informational]` and excluded from the verdict — the structurally-not-applicable cases: no `nakon-config.json` (plant coverage), no `domain_roles.json` (domains), no `injects/` dir, `--expect-no-vulns`, fewer than 2 teams for misconfig-survival. Each `GateResult` also carries the SUMMARY `label`, so the two cannot drift apart (the old hand-printed SUMMARY said "SKIP — unverified" for isolation while the dict recorded a FAIL). |
| `--allow-unverified <gate>` | The explicit waiver for one gate's SKIP: repeatable, and it waives **only** SKIP (a FAIL always fails). Names that match no gate in the run are warned about, not silently accepted. |
| `--timeout SECONDS` | Optional whole-run wall-clock budget (default `0` = disabled). Deliberately cooperative: checked **between** gates, never mid-flight, so it can never kill an ssh or interrupt a Proxmox task; worst-case overshoot is the one gate already running. A gate skipped on budget is recorded `SKIP_UNAVAILABLE` — so a budget can bound a verify but can never turn an unevaluated range into a PASS. |
| TLS warnings | Silenced: the engine speaks plain HTTP, but Quotient's checks and the Proxmox API elsewhere use self-signed TLS, and warnings would drown the output. |
| box authentication | Boxes authenticate with the Proxmox key (cloud-init authorizes it for the configured `box_username`); the box password is informational here. The admin password is parsed from `credentials.txt`, falling back to `changeme123` (overridable with `--admin-password`); engine IP from `terraform output -json` (`--engine-ip`). |
| `MISCONFIG_CHECKS` | Each entry documents what the planted misconfig looks like and how to verify it: `suid-find` (the `s` in the owner-exec slot of `ls -l $(which find)`), `www-data-shell` (`/etc/passwd` line ends `/bin/bash`), `bad-perms-userConfig` (`/etc/shadow` mode 666), `writable-sudoers` (`/etc/sudoers.d` mode 777 — nakon's `004-writable-sudoers.sh`). Only a handful are mapped; the spot-check picks a box/config pair it can actually verify. |
| `check_no_default_creds()` | False-positive guard: only the actual value token (last whitespace-separated field) is compared against the default literals, not the whole line — `box-login (ubuntu)  <password>` legitimately contains the literal `ubuntu` as the non-secret username label, which is not a rotation failure. |
| `check_services()` | Returns a **list** of GateResults (the service gate plus `pins_registered` when pins exist); every path returns that shape, so a half-deployed engine no longer crashes main with a tuple-unpack `ValueError` that suppressed the SUMMARY. A service with no scored rounds yet is "not yet scored", not a failure (excluded from the UP/DOWN tally). Check `Result` values are normalized (the string `"false"`/`"0"` count as failed) since Quotient returns strings. DOWN is reported but not fatal unless `--strict-services`; under strict, nothing-scored is a FAIL (not a vacuous pass) and the newest scored round must be within 5 × 60 s `Delay`. Pins always gate. |
| `check_isolation()` | The DROP rule match requires **both `-s` and `-d`** (`192.168.0.0/16` appears ≥ 2 times). With ≥ 2 teams it also runs a live cross-team connection test (expected blocked) plus an internet-reachability control **and** a target-liveness probe: a dead target and a blocked one look identical from here, so a probe that cannot establish all three is SKIP, never a verified pass — "the rule-presence pass stands" is exactly the fail-open this replaced. |
| `check_misconfig_survival()` | Groups are keyed on the **normalized config NAMES** (a tuple of `{"name": …}`-extracted strings), not raw entries: dict-form entries are unhashable, and "the same box, different teams" means the same names regardless of `vars`. Present on some teams but missing on others FAILs (did not survive cloning), and **absent on every team now FAILs too** (the plant never landed anywhere — the old code matched no branch and left `all_ok` True). All probes unprovable (SSH dead) is SKIP, never a pass. |
| `check_domains()` | The live replacement for the freeze's operator attestation. Per team: the DC answers `Get-ADDomain` for `team<id>.local` **with a syntactically valid `S-1-5-21-*` DomainSID** (a promoted DC that answers without one fails — with one team, uniqueness alone is vacuous, so the DSID shape is what actually gates), the planted `svc-support` account exists, and every member is joined (Windows `PartOfDomain`+domain, Linux realmd). Returns a GateResult; probes are one per VM on the MAX_CONCURRENCY (8) pool. Details below. |
| `check_plant_coverage()` | Fails **closed**: it reads `.deploy_state.json["plant_coverage_failed"]` per-machine expected-vs-planted, falls back to the `nakon_failed_steps` tally the deploy promises, and when neither source exists it FAILs ("coverage was never recorded") instead of printing PASS — missing state, an older state, or a nakon with no `--json` outcome used to leave `failed = {}` and pass vacuously while its own SUMMARY warned. A non-empty tally can never PASS. `deploy_lib/coverage.record_coverage` clears a machine's stale failures when its stage replants clean. The **golden plant records too** (phase 4, via build_golden_set's `coverage` callback): `{box}-golden` keys on slot 0, `{box}-golden-slot{N}` on satellite slots (every slot's stage config names its machine identically, so the keys must not collide), recorded on every completion path — a skip (reuse/checkpoint/pristine/cold) guarantees the record exists without erasing a prior failure, and an `alpine_services`-tolerated failure lands in the record since it is a real gap on the golden disk. A failure on ANY slot's golden flags every team copy of the box. Returns a GateResult, never a bare tuple. |
| `check_round_loop(fix=…)` | Now **consumed by main**: a stopped round loop used to WARN and return True while main discarded the value, so a frozen scoreboard never affected the exit code (pfsense-ad 2026-09-28). It FAILs for the current run; `--fix-round-loop` still POSTs the start/unpause pair, but re-run verify to confirm a fresh round. |
| exit-code gate | `isolation` and `misconfig_survival` are NOT optional: a failed isolation check means teams can reach each other *right now*, and a misconfig missing on one team's clone breaks the "every team defends the same misconfigs" fairness guarantee. Down services are soft unless `--strict-services`; logins, the default-creds guard, misconfig spot-check, injects, pins, plant coverage, round loop, and domains (when checked) gate as well. Any non-waived SKIP fails the run, so a verify that used to exit 0 can now exit 1 for a gate that never ran. `report_healthcheck_status`/`report_beacons` are informational only. |
| verify concurrency | The three big remote loops — `check_domains`, `check_misconfig_survival` and `report_beacons` — run on the full MAX_CONCURRENCY (8) pool. They are pure SSH / guest-agent probes with no Proxmox task and no datastore write, and 8 stays far under the engine's raised sshd `MaxSessions` 64. The aggregation is re-serialised in the old order, so every status, message and exit code is unchanged (`tests/test_parallel_verify.py`). |

**`check_domains()` — cross-team and input rules.** Duplicate **DomainSIDs** FAIL (promotion reused
image state — winad-testrun 2026-09-25); duplicate member machine SIDs are INFO only (linked clones of
one golden, harmless for isolated forests). A single-team lineup PASSes with "uniqueness needs a second
team". Fail-closed on input: a present-but-malformed `domain_roles.json`, a role value outside
`dc`/`member`, or a role box absent from `boxes.json` FAIL the gate — only file absence skips.

## generate-packet.py

Competitor briefing packet renderer — pure local-file → Markdown, no live infrastructure.

- **Deliberate scope** — the packet is intentionally the SAME document for every team (no team-specific
  data; real per-team credentials are issued separately at competition start via `credentials.txt`) and
  intentionally omits `box_vulns.json` — nakon's planted misconfigs would spoil the competition.
  `box_vulns.json` is deliberately never read here.
- **`_SERVICE_DISPLAY`** — mirrors `quotient/setup.py`'s `_SERVICE_TO_CHECK` Display fields so the packet
  shows the same human-readable service names the scoreboard does (e.g. `apache` → `http`) instead of raw
  catalog identifiers. Kept as a separate, smaller table rather than importing the full dict: this script
  has no dependency on Quotient's TOML check shapes — just the names a competitor recognizes.
- **`load_inject_schedule()`** — titles + timing offsets only, never inject content/description; same
  `inject.json` shape as `config_ops.load_injects()`.
- Works the moment `Compfile`/`boxes.json`/`box_services.json` exist — from a partial create-competition
  run or fully hand-authored (error messages point at the relevant usage-agents sections).

## packet_ops / compile-packet / run-schedule

- **`packet_ops.validate_profile()`** — surfaces at compile time everything that would die mid-deploy:
  comp-name/unix-username validity and legacy-account collisions, domain `name_template` shape,
  credlist/domain cross-requirements, box count/name/octet/template/fidelity/role rules, exactly one DC,
  service pins and display uniqueness, inject offsets. **Template validity is only "non-empty"** — whether
  the named template resolves on the target node is checked by the deploy preflight
  (`preflight.run_preflight`, via `config_ops.preflight_gates`), not here.
- **`packet_ops` emits** `Compfile` (with `domain_prefix`/`domain_suffix`/`packet_source`), `boxes.json`,
  `users.json`, `box_services.json`, `domain_roles.json`, `injects/`, `packet-fidelity.md`, and three
  0600 secret files: `passwords.json`, `domain_accounts.json`, `box_baseline.json`.
- **Score-only pins** (`score/tcp`) score a NATIVE service the catalog cannot plant; stripped from the
  nakon machine list and the catalog check, present in `event.conf` and `expected_service_names`.
- **`run-schedule.py`** — the engine has no schedule model; pausing IS the schedule. Reads a packet's
  `schedule:` windows and (with `--execute`) drives `start`/`freeze`/`resume`/`end`; `end` captures the
  final scoreboard + injects into `<comp>/evidence/` BEFORE pausing.

## Multinode & sync modules

Multi-node is documented end-to-end in [multi-node.md](multi-node.md); these are the internals notes.

| Module | Note |
|---|---|
| `nodes_ops.py` (facade: `nodes_config.py` / `placement_record.py` / `placement_planner.py`) | One competition spans up to `MAX_SATELLITES`+1 hosts. Placement is capacity-fill; per-host API tokens are referenced by **env-var name** (`nodes.json` records `token_env`); `activate_placement()` swaps the `TF_VAR_proxmox_*` env for the engine's host before anything node-scoped, restoring the originals afterward. An existing `placement.json` is authoritative; a resume without one adopts its deployed endpoint rather than re-balancing a live range. `golden_vmid_for_slot()` shifts the golden block by one box-stride per satellite (`engine_vmid + 150 + slot*10 + box_idx`) because linked clones cannot cross hosts on separate storages; `jump_vmid_for()` is `engine_vmid + 130 + slot`. |
| `jump_ops.py` | Per-satellite alpine jump/router VM. It impersonates the engine at L3 on that satellite's team bridges (holds `192.168.<id>.1`, the address every box knows as its gateway), DNATs the gateway IP's apt-cacher port to the engine, and SNATs engine→box traffic so the boxes' gateway-IP-only SSH trust still sees their gateway. Forwarding is default-DROP with explicit accepts, preserving team-to-team isolation. NIC hotplug past the first NIC is unreliable — reboot, do not hotplug. |
| `routing_ops.py` | `verify_satellite_routing()` is the fail-loud convergence gate after apply #1: nothing downstream (satellite golden plants, nakon, scoring) works without engine → jump → satellite-bridge paths. The static routes themselves are written into the engine's netplan by `team_nics` from `placement["satellite_routes"]` (plus a oneshot systemd unit so an operator engine reboot mid-event re-asserts them instead of silently stranding satellite scoring); this module only proves them. |
| `template_sync_ops.py` / `sync-template.py` | Cross-node template copy over root SSH (`vzdump --stdout \| ssh qmrestore`). Satellites need their own copies of every box template plus an alpine base for the jump VM clone before they can host teams; the per-node template gates and placement probes point here on failure. |

## quotient/setup.py

Drives Quotient: `event.conf` generation, team seeding, engine unpause, inject creation.

| Topic | Note |
|---|---|
| module split | `unpause_engine()` is separate from `seed_teams()` because unpausing is not safely repeatable (hence deploy's `engine_unpaused` state flag), while seeding is idempotent. |
| `_SERVICE_TO_CHECK` | Maps a nakon service name to its Quotient check key + config dict. Keys and field names must match Quotient's Go struct TOML tags exactly (the check-type key on `Box` is case-sensitive; field names are matched case-insensitively by BurntSushi). A box accumulates multiple checks (apache + bind → Web + Dns); vulns and unrecognized service names are skipped with a warning. Per-family notes below. |
| `build_event_conf()` | Box IPs use `192.168._.<last_octet>` (the wildcard every team's check matches). `StartPaused = true` holds scoring until `unpause_engine()`; `Delay 60`/`Jitter 10`/`Points 5` are the round cadence and per-check value. Check identity per box is the `Display`; details below. |
| `_normalize_host()` / `_admin_session()` | Quotient's address comes off Terraform as a bare IP, and requests needs a scheme. Auth is cookie-based (`POST /api/login` sets a session cookie); there is no bearer token anywhere in the API. |
| `seed_teams()` / `unpause_engine()` / `create_injects()` | `seed_teams` looks team IDs up by name, batch-updates identifiers + `active`, and sets the competition `started` flag (idempotent). `unpause_engine` unblocks the round loop and is **not idempotent** — gate it with a state flag. `create_injects` is a multipart POST per inject, skipping titles already present; if existing injects cannot be fetched it proceeds without dedup and says so (a resume may create duplicates). |

**`_SERVICE_TO_CHECK` per family.**

- **Web** — `Url` is a required nested array of `{Path, Status}`.
- **Dns** — `Record` is a required array of `{Kind, Domain, Answer}`.
- **Ssh** — `CredLists` is required for the login check.
- **Ftp** — authenticates against `linux.credlist` (the same accounts SMTP scores against): the
  `unauthorized-ftp-server` config installs vsftpd with Ubuntu's default `anonymous_enable=NO` /
  `local_enable=YES`, so an anonymous check can never pass but a local login does. **Windows FTPS is the
  exception**: the planted site answers `534 Policy requires SSL`, so the CDE profile plants it
  (`plant_only`) and scores port 21 with a score-only Tcp check.
- **Smtp** — `smtp.go` always calls `getCreds`, so `CredLists` is required.
- **Imap** — `CredLists` triggers the authenticated mailbox-list check.
- **Sql** — `Kind` defaults to `"mysql"` but must be explicit; these authenticate against the database's
  own user table, not a system account. `fix_services_on_boxes()` binds mariadb/mysql to `0.0.0.0` and
  creates the credlist DB users (`CREATE USER … @'%'` + `GRANT ALL`), so the same credlist that
  satisfies SSH/SMTP logs in here. Do not drop `CredLists`.
- **Telnet** — no protocol-aware check exists, but the generic `Box.Tcp` check (dials the port, scores UP
  on connect) is exactly enough. Confirmed against `/opt/quotient`'s source and
  `config/event.conf.example` on the engine (2026-08-07).
- **Windows entries** (`Enable WinRM`, `New SMB Share`, `RDP misconfigs`, `IIS HTTP`) — Quotient has no
  SMB/RDP/WinRM-aware check type at all (only Web/Dns/Ssh/Ftp/Smtp/Imap/Sql/Tcp), so those use the
  generic Tcp port-open check; `IIS HTTP` is the exception (a real Web check, `Url / → 200`). Keyed by
  the exact nakon catalog config name, not a generic binary name.
- **`ADDS`** — scored (Tcp :389) but never planted from `box_services.json`; see
  `DOMAIN_INFRA_CONFIGS`. Scoreboard ServiceName is `<box>-<Display>`.

**`build_event_conf()` details.** The `inject` account is emitted whenever an inject password is
supplied (i.e. the competition has an `injects/` dir): Quotient's INJECTAUTH-guarded routes accept
`admin` and `inject` roles, so a dedicated inject manager lets an organizer run injects without the admin
login. Quotient holds a slice of checks per type and names each `<box>-<Display>`, with duplicates a
config-load error, so multiple same-TYPE pins on one box are fine as long as Displays differ (a collision
is a generate-time `SystemExit`, never a silent drop — see known-issues regression-4x1). Dict pins take
per-check overrides (`PIN_CHECK_OVERRIDES`) merged over the base config; nakon machine lists strip them,
so one catalog config plants once but can score under several Displays. `expected_service_names()` shares
the pin resolution for verify's `pins_registered` gate. `CredlistSettings` is emitted only when some check
needs it — box-level `credlists` entries are just names, resolved by Quotient against the top-level
registry.

## terraform/main.tf

Team bridges, the scoring VM, every team's boxes, NIC wiring, cold-boot + netplan.

| Symbol | Note |
|---|---|
| `local.proxmox_host` | bpg otherwise asks the API for each node's address for its SSH ops, which may not be reachable from the operator (it can return a LAN IP while the operator only has Tailscale); the endpoint host is used instead. |
| provider `ssh` block | Used by bpg for operations the REST API cannot do (e.g. file uploads). `insecure = true` for the self-signed cert most Proxmox installs have. |
| `team_bridge` (+ `_sat1..4`) | **No `ports` attribute**: an empty bridge has no uplink, so teams cannot escape onto the LAN. Slot 0 is the engine node's; `_satN` carry the satellite provider alias. |
| `transit_bridge` | `vmbrW<id>` per slot-0 team, **only when a box is `in_path`** (`local.has_in_path_fw`) — otherwise the resource's `for_each` is empty and every plan is byte-identical to the pre-firewall pipeline. The engine's transit NICs are appended AFTER the team NICs so the positional netplan names never shift. |
| `scoring_engine.vm_id` | `var.scoring_vm_id` — configurable, `1000` only as the default. |
| `agent { enabled = true }` | Needed to read back the real management IP; team-NIC DHCP and the build VM still use it. |
| no cloud-init on the engine | The template's baked-in netplan (dhcp4 on the mgmt NIC) suffices, and its own cloud-init build already baked in `var.ssh_public_key` and passwordless sudo for `var.vm_username`. **Superseded 2026-09-29: the engine mgmt IP is STATIC by default** (`10.0.0.250`, written into tfvars by deploy; `TF_VAR_engine_mgmt_ip=''` restores DHCP) because the DHCP engine rebooted onto a different address mid-event while terraform's saved output stayed stale (shakedown-5x4: .221→.243→.233). `template_ops` also uses it for the build VM's `ipconfig0`. |
| `team_box` + `team_box_sat1..4` | **Five slot-dimensioned resources**; `for_each = var.build_team_boxes ? local.all_team_vms_by_slot[<n>] : {}`. apply #1 has `build_team_boxes=false`, apply #2 sets it true. **Every team is Terraform-managed** — there is no API-clone path in v2. |
| `local.all_team_vms_by_slot` / `all_team_vms` | Per-slot maps of team→box entries carrying `ip`/`gw`/`bridge` derived from `team.identifier` + `box.last_octet`, matching `enumerate_targets()`. Slot 0 keeps the historical `all_team_vms` name. Keys keep the historical naming (`team1-<box>`, `<identifier>-<box>`). |
| `clone { full = false }` (team boxes) | Every team box is a **linked** clone of its golden: metadata work, seconds. **No `retries` is set** — templates are not locked the way a clone-source VM is, so the old `retries = 15` budget is gone (the remaining full clones — engine template and goldens — are built one at a time operator-side). |
| `lifecycle` precondition | The computed `team_box` vmid is checked at plan time against `var.scoring_vm_id`, so a stride collision fails fast instead of producing an API conflict at apply. It also asserts `golden_template_ids` has one entry per box when `build_team_boxes` is true, and that at most ONE box is `in_path` (the transit design routes every team through a single gateway). |
| `template_ids` local | Resolves template names from a data source filtered on the `template` tag, **excluding running VMs** — a running box with a stray `template` tag duped names and failed every apply (live-confirmed 2026-09-24). |
| `dynamic "disk"` | Emitted only when `disk_gb` is set: omitting it keeps the template's own disk, and specifying null makes bpg default to 8 GB, undercutting any real template disk and triggering an unsupported-shrink error. |
| disk interface | `coalesce`, not `try`: a missing optional attribute is `null`, not an error, so `try(null, "scsi0")` returns null and fails validation at apply; `coalesce(null, "scsi0")` yields the default. |
| `dynamic "initialization"` | Emitted only for non-Windows **and non-unmanaged**: a Windows template has no cloud-init agent to consume the block, and an unmanaged appliance (pfSense) ignores cloud-init — it self-configures via firewall_ops' console bootstrap. |
| `user_account.password` | `var.box_password` — nakon's paramiko connections use password auth; without it the cloud-init account has no password hash and every login is rejected. Generated fresh per competition, never a literal. |
| `dns.servers` | Always a public resolver, never the team's `dns*` box: pointing boxes at that box deadlocks provisioning, because its bind9 is installed by nakon via apt-get, which needs a resolver that already works. `fix_dns_on_boxes()` forced 8.8.8.8 over the top anyway, so the `dns*` box never actually served its team. To make it the real resolver, repoint the boxes after nakon has run. |
| `scoring_mgmt_ips` exclusions | Loopback, `192.168.` (team subnets), Docker's `172.16/12` (Quotient runs in Docker on this VM), and Tailscale's CGNAT `100.64/10`. No ordering guarantee — the first survivor is taken. |
| `null_resource.team_nics` | Triggers carry **identifiers only**: `var.teams` there would print team passwords in every plan. NICs are named predictably (ens18=mgmt, ens19=team1, ens20=team2, …; with a firewall lineup the transit NICs continue the sequence at ens<19+n>; the firewall's cutover later rewrites this file — see firewall_ops); netplan **refuses world-readable configs**, hence `chmod 600`; sysctl sets `ip_forward=1` and `rp_filter=2`. Depends on the reboot resource: **the hypervisor-level cold boot must complete before `netplan apply` can find ens19/ens20**. In multi-node it also writes the satellite routes. |
| `null_resource.reboot_scoring_engine` | A **cold boot is required: a guest reboot does not trigger the PCI scan** that detects newly attached VirtIO NICs; the VM is hard-stopped (`shutdown=0`) and started via the Proxmox API. |
| `null_resource.orchestrate` | SSH-readiness probe against the **scoring engine** (`local.scoring_ip`), 30 attempts × 10 s plus a 30 s settle. Depends on the reboot (it must complete first) and on `team_nics`. |
| `team1_key = "team1"` | The anchor team is looked up **by name, not sort order**, so a non-default team-key set fails fast instead of silently building the wrong team. |
| satellites | Exactly `MAX_SATELLITES=4` provider aliases exist; unused slots carry dummy settings and are never configured because no resource references them. |

## terraform/variables.tf

TF_VAR inputs; the long descriptions carry rationale worth preserving.

| Variable | Note |
|---|---|
| `vm_username` | The built-in OS account already present on every template (not provisioned by us). |
| `box_username` | The cloud-init account created on every team box; themeable per competition via `competitions/<id>/users.json` (`create-competition.py` writes `TF_VAR_box_username`); defaults to `ubuntu`. |
| `box_password` | Generated fresh (or carried from packet/prior state — see `deploy.py`), not a fixed literal, because nakon authenticates with password auth. Only this password rotates; the username may vary per competition. |
| `teams` defaults | Placeholders (the pipeline always overwrites `TF_VAR_teams`), but kept realistic (`101`/`102`) so anyone hand-running `terraform apply` builds addressing consistent with the team subnets at 101+ that the engine's NAT/isolation rules expect. |
| `boxes_per_team.disk_gb` | Omitting it keeps the template's disk size **and** leaves the disk on the template's storage pool: `var.datastore` only applies when the disk block is emitted. |
| `boxes_per_team.disk_iface` | Defaults to `scsi0`; set `"sata0"` for Windows templates that boot from SATA (scsi0 needs virtio-scsi drivers the image may lack). |
| `boxes_per_team.template` | Must match a Proxmox VM tagged `template` exactly (see usage-people's "Adding a template VM"). |
| `scoring_vm_id` / `engine_mgmt_ip` / `engine_mgmt_gw` / `teams` / `team_identifiers` | See the env-var reference in [usage-people.md](usage-people.md#configure-the-event). |

## Conventions

Repo-wide rules the code (and AGENTS.md) enforce; repeated here because every module above depends on
them.

- **nakon is a CLI dependency only.** All catalog access goes through `nakon` subprocesses
  (`randomize`/`build`/`deploy`), never an in-process import and never a direct MySQL connection.
- **`vendor/nakon` is pinned to a release tag** and bumped deliberately: check out the tag in the
  submodule, then commit the new pointer. Do not develop nakon inside this checkout — work in the nakon
  repo, tag a release, then pin it here.
- **`vendor/nakon/.env`** (gitignored) holds the vulndb creds for build/randomize time; the bundle itself
  carries no credentials to the engine. **`NAKON_DIR = Path("vendor/nakon")`** is the cwd for nakon
  subprocesses; bundles live under `vendor/nakon/bundles/`, content-addressed and shared across
  competitions.
- **Per-run secrets are gitignored** (`teams.json`, `event.conf`, `credentials.txt`, `nakon-config.json`,
  `.deploy_state.json`); non-secret artifacts are tracked (`boxes.json`, `Compfile`, `box_services.json`).
- **`--from-phase N` resumability** — pinned `box_services.json`/`box_vulns.json` make re-runs
  deterministic: the same selection produces the same bundle-cache hit, so a resumed deploy replays
  instead of re-randomizing.

## artifacts_lib — per-run test artifacts

`artifacts_ops.py` is only a facade re-exporting the public names of `artifacts_lib/` (plus
`shutil`/`subprocess`, which tests patch through it). One concern per module, shared logic defined
once:

| module | owns |
|---|---|
| `constants` | file names, status vocabulary (`OK`..`SKIPPED`, `LOST`), `SIDES`, `Unreachable`, `REPO` |
| `env` | `iso`, `git_facts`, `in_worktree` |
| `paths` | `artifacts_root`, `test_dir`, `test_key`, `run_id_from_state`, `comp_name` |
| `hashing` | `sha256_file`, `file_facts`, `seal_local_file`, `write_bytes_atomic` |
| `manifest` | test.json: `ensure_test`, `record_phase/paths`, `update_manifest`, `resolve_recorded` |
| `plan` | `plan_targets` (pure), `want_label` (the collection record key) |
| `transport` | scp / guest-agent / local / ssh-cmd fetchers, `fetch` dispatch, shared `_ssh_options` |
| `collect` | `collect`, `load_collection`, canonical-document derivation |
| `verdict` | `ingest_verdict` (parses scrim-report's INTERACTION.md), `ensure_verdict` |
| `status` | `document_state`, `warn_summary`, `side_status`, `why_missing` |
| `report` | `write_stub`, `render_report_skeleton`, `write_report_skeleton` |
| `index` | `update_index`, `list_tests` |
| `archive` / `seal` | `archive_test`, `default_archive_root` / `verify_test`, `seal_test` |
| `lifecycle` | `finalize`, `collect_for_teardown` |
| `cli_common` / `cli_read` / `cli_write` | test-artifacts.py: resolution + formatting / list, show, plan / verify, collect, archive |

Patch-compat note: `lifecycle.collect_for_teardown` resolves its default transport through
`artifacts_ops.default_transport` at call time, so `patch.object(destroy.artifacts_ops,
"default_transport", ...)` still steers teardown. The other cross-module calls bind inside the
package; patch the module that owns the function (or call through the facade, as the scrim harness
does).
