# Internals — per-module design notes, invariants, and rationale

Design notes and invariants extracted from the deploy-pipeline code and Terraform (extraction date:
2026-09-21). This is the "why" behind individual symbols, constants, timeouts, orderings, and
workarounds — the content that used to live only as comments and docstrings next to the code.
`docs/architecture.md` owns the system-level view (overview, seven phases, component map, data
flow, network/isolation model, snapshot table, nakon contract, secrets inventory); this doc
deliberately does not repeat it. Full incident write-ups live in `docs/known-issues.md`; only the
design consequence is repeated here.

## constants.py

Single source of truth for VM identity math, snapshot names, nakon budgets, and naming; shared by
`range_ops`, `deploy`, `redeploy`, and `verify`.

- **`vm_id_for` stride (mirrored in `terraform/main.tf`)** — VM IDs are `200 + identifier*10 +
  box_index`, stride 10. `terraform/main.tf` computes the same arithmetic inline; changing one
  without the other creates ID collisions. `range_ops.vm_id_for()` is the sole Python derivation.
- **`MAX_BOXES_PER_TEAM = 10`** — box index 0–9 fits the stride-10 block each team owns.
- **`MAX_TEAMS = 154`** — team identifiers `101`–`254` map to `192.168.101.0/24` …
  `192.168.254.0/24`; `deploy()` validates `--teams` against this ("team identifiers are
  192.168.<101-254>.x").
- **`SCORING_ENGINE_VMID = 1000`** — the engine's fixed vmid, hardcoded in `main.tf` too (100
  collides with an existing unrelated VM on this node). Phase 1 destroys it explicitly because
  Terraform alone isn't trusted to have it in state on a rerun.
- **`SNAP_BASE = "tz-base"` / `SNAP_READY = "tz-ready"`** — the two snapshot names every consumer
  (`clone_ops`, `redeploy-competition.py`) keys on.
- **`PER_MACHINE_NAKON_BUDGET = 2400`** — seconds budgeted per machine in a `nakon deploy` pass;
  `run_nakon` timeouts are `max(2400, budget * machine_count)`.
- **`SLOW_SERVICES = ("splunk", "roundcube")`** — excluded from auto randomize (passed as
  `--exclude` to `nakon randomize`); both are heavy/slow installs.
- **`DISRUPTIVE_CONFIGS`** (`resolv-conf-null-dns`, `apt-sources-empty`, `apt-hold-all-packages`,
  `dpkg-broken-hold-state`) — the set `generate_nakon_config()` sorts to the end of each machine's
  configuration list (see `nakon_ops`).
- **`WINDOWS_ADMIN_USER = "Administrator"`** — the sysprepped local admin nakon
  authenticates as on Windows boxes.
- **`NAKON_DIR = Path("vendor/nakon")`** — nakon subprocesses run with this as cwd (so it can read
  its gitignored `.env`); bundles land under `vendor/nakon/bundles/`.

## utils.py

Compfile/users.json loading, DNS-fix command strings, competition picking. No Proxmox access.

- **`DNS_FIX_CMD`** — cloud-init ignores `dns.servers` when the IP is static, so the cloud-init
  DNS block alone doesn't stick; this repairs it live via `/etc/resolv.conf` +
  `/etc/resolv.conf.head` + a `systemd/resolved.conf.d/upstream.conf` drop-in + `systemctl restart
  systemd-resolved` (all pointing at 8.8.8.8).
- **`DNS_FIX_CMD_ROOT`** — the same fix with `sudo ` stripped, for the QEMU-guest-agent path,
  which already runs as root: no sudo, and no working network or sudoers needed — that's the whole
  point of the fallback.
- **`BOX_USERNAME_DEFAULT = "ubuntu"` / `CREDLIST_USERNAMES_DEFAULT = ["admin", "user1", "user2"]`**
  — fallbacks when no `users.json` exists, matching every pre-existing competition's behavior;
  themeable per competition since.
- **`load_users_config()`** — reads themeable box login + 3 credlist usernames from
  `competitions/<id>/users.json`, falling back to the defaults per-field.
- **`load_compfile()`** — key=value parser; skips blank lines and keys with no value; non-numeric
  difficulty degrades to 0 rather than raising.
- **`compfile_flag()`** — integer Compfile knob reader (e.g. `team_beacons 1`); returns the
  default when the key or the file is absent so old Compfiles keep their behavior.
- **`pick_competition()`** — shared interactive picker used by destroy/redeploy.

## ssh_ops.py

Gateway SSH (`ProxyCommand -W`), Terraform context, and wait helpers. Shared by create/redeploy/
verify — single source of truth; `verify-competition.py` keeps a local mirror only because it must
run without the package import.

- **`is_windows_template()`** — `"win" in template_name.lower()`; one of three identical
  definitions (see `create-competition.py` below for why all three must agree).
- **`read_terraform_ctx()`** — parses `agent_context` out of `terraform output -json`; resolves a
  relative `ssh_key_path` to absolute (relative to `terraform/`, since that's the provider's
  working dir).
- **`ssh_via_gateway()`** — reaches team boxes through the engine with `ProxyCommand ssh -W %h:%p`.
  Requires `AllowTcpForwarding=yes` on the gateway (engine_ops sets it explicitly). Linux boxes
  authenticate with the Proxmox key as `box_username`; Windows uses password auth.
- **Engine connection multiplexing (M1.5)** — `engine_ssh_opts` / `_engine_opts(host=…)` add
  `ControlMaster=auto` + `ControlPath=~/.tezcatlipoca/cm-<ip>-22-%r` + `ControlPersist=900`:
  every per-box ProxyCommand and every direct engine call reuses ONE authenticated engine
  connection instead of a fresh double handshake per call. Two coupled requirements:
  `forget_engine_host_key` retires the master socket (a surviving master would keep the old
  host key after a rebuild), and engine_ops raises sshd `MaxSessions` to 64 (default 10 would
  throttle M2's concurrent waits sharing the master) plus `MaxStartups 30:30:100` for
  cold-start bursts.
- **`ssh_on_gateway()` / `ssh_to_engine()`** — direct SSH to the engine itself (no hop — it *is*
  the gateway); the latter is an alias kept for readability.
- **`wait_for_ssh()` / `wait_for_http()`** — poll-based, never raise, warn and continue on
  timeout (deploy aborts only when *every* target of a phase is unreachable).
- **`wait_for_boxes_ssh()`** — polls each target through the gateway; Windows boxes are probed via
  guest agent (no key auth exists for them). Raises only if **all** boxes fail: that looks
  systemic (see each box's guest-agent diagnosis), not a one-off timing fluke — aborting beats
  burning through the DNS/auth/nakon retry loops for boxes already known unreachable.
- **`wait_for_cloud_init()`** — cloud-init can revert planted perms (sudoers, sshd_config) after
  clone, so it must finish before nakon plants anything. Windows boxes are skipped — no
  cloud-init there; `bootstrap_windows_box()` is its equivalent. Exit code 2 means done with
  non-fatal warnings — still finished, still success.

## range_ops.py

Proxmox API layer, VM identity math, guest-agent exec, VM lifecycle, and the `(team, box)` target
abstraction. Reads `TF_VAR_proxmox_*` from the environment; deliberately no prompts or phase
logic.

- **API token + `verify=False`** — Proxmox's API-token auth talks straight to the API over the
  same self-signed cert `main.tf`'s provider block sets `insecure = true` for: same tradeoff, just
  from Python. Each entry point that imports this module calls `urllib3.disable_warnings()`.
- **`proxmox_api()`** — retries transient connection blips (pveproxy drops) up to 4 attempts with
  a short backoff, on `ConnectionError`/`Timeout` only, so real HTTP failures aren't masked.
- **`wait_for_proxmox_task()` (timeout 1800)** — must tolerate slow storage: clones and deletes
  can exceed 10 minutes on this host.
- **`diagnose_unreachable_box()`** — guest-agent (virtio-serial, no network needed) report of the
  box's IPv4 state and `cloud-init status --long`. Never raises; used to turn "SSH failed 8x"
  into an actionable line in the failure handler.
- **`guest_agent_exec_root()`** — runs bash as root via the QEMU guest agent over virtio-serial;
  no sudo needed, so it bypasses broken sudo (writable-sudoers, ungranted NOPASSWD) and needs no
  network. Returns `(rc, out, err)` but raises on agent-level failure.
- **`guest_agent_exec_windows()`** — PowerShell as SYSTEM via the agent; `-EncodedCommand`
  (UTF-16LE base64) avoids quoting issues. The only channel into a Windows box before bootstrap.
- **`wait_for_guest_agent()`** — agent ping loop; virtio-serial works with no network. Never
  raises (returns bool) so callers decide whether absence is fatal.
- **`vm_id_for()`** — the sole Python derivation of `200 + identifier*10 + box_index`.
- **`stop_vm()`** — graceful ACPI shutdown first for filesystem consistency, forced stop only if
  ACPI is ignored.
- **`start_vm()`** — no-op for a VM that doesn't exist or is already running.
- **`enumerate_targets()`** — one entry per `(team, box)` with vmid/IP/name pre-derived.
  Invariant: **build from the full lists, then filter — never filter `boxes` and re-enumerate**,
  because `vmid` is positional in the box list; a filtered re-enumeration assigns wrong IDs and
  collides with existing VMs. `vm_name` records what the VM is actually called in Proxmox
  (`team1-<box>` for Terraform-created team1 vs `<identifier>-<box>` for API clones — see
  `clone_ops`) so nothing re-derives it; `machine` is `<box>-team<identifier>`, nakon's
  opposite-order naming.
- **Snapshot semantics** — `tz-base` = booted, networked, pre-nakon; `tz-ready` = as-delivered
  post-nakon+hardening. Team2+ `tz-base` is taken *after* cloning (post phase 5), so it carries
  team1's configs on the cloned disk but is still that team's per-box "before nakon" point.
- **`destroy_vm_if_exists()`** — stop (await) then delete (await). With `expect_tags` set, the
  parallel cleanup sweep first checks the VM's config tags and **refuses to destroy a tagged VM
  whose tags lack `tezcatlipoca` + `comp-<name>`** — a stray VM that happens to occupy a
  computed vmid is never touched. Untagged VMs pass with a warning: the transition window
  covers ranges built before tagging existed (main.tf and clone_ops both tag VMs now), and the
  preflight vmid-clash gate is the other half of the ownership proof. Any other caller (redeploy
  rebuild) passes no `expect_tags` and keeps the old unconditional behavior.
- **Phase-1 cleanup runs as a worker pool of ≤8** over the ownership set: computed team vmids,
  the engine vmid, and everything in `cloned_vms.json` (deliberately broader than Terraform
  state — the clones are not in state, and skipping them would let phase 6 "resume" onto stale
  disks with the previous run's passwords). Deletes are metadata-light on the API; the
  datastore-saturation hazard (HTTP 596 / pvestatd hang) belongs to bulk clone *writes*, which
  stay serial. Bridges stay serial after the pool. Wall time drops from the sum of ACPI
  shutdowns to the slowest one.
- **`list_snapshots()`** — returns an empty set when the VM is gone or the API refuses: callers
  treat "no snapshot" and "couldn't ask" the same way and fall back to a mode that doesn't need
  one.
- **`take_snapshot()`** — disk-only (`vmstate 0`, fsfreeze via guest agent); deletes an existing
  snapshot of the same name first (replace semantics). **Never raises** — snapshots are optional
  recovery, and thick LVM simply can't snapshot at all.
- **`rollback_snapshot()`** — requires a stop/start cycle because snapshots hold no RAM; raises on
  failure (unlike `take_snapshot`).
- **`snapshot_support_hint()`** — the one-line explanation for missing snapshots: either the range
  predates snapshotting or the datastore can't (thick LVM can't; qcow2-on-file, ZFS, LVM-thin,
  Ceph can), pointing at `--mode reconfigure` / `--mode rebuild` as the fallback.

## config_ops.py

Competition configuration: prompts, `Compfile`/`boxes.json`/`teams`/`users.json`/injects
persistence, `.env` updates.

- **`update_env()`** — rewrites `TF_VAR_*` lines in `.env` in place (preserving comments and
  order) **and** mirrors each value into `os.environ`, because the `terraform` subprocess calls
  later in the same process read the environment that was loaded once at import time — before
  these values were known. The replacement line is passed to `re.subn` as a **function, not a
  string**: `re.sub` interprets backslashes and group refs (`\1`, `\g<0>`) in a string
  replacement, which would crash or corrupt `.env` for free-form values (e.g. an `event_name`
  containing a backslash).
- **`list_proxmox_templates()`** — mirrors `main.tf`'s own lookup (`data
  .proxmox_virtual_environment_vms.templates` filters on the `template` tag alone) so the picker
  only ever offers names Terraform will actually resolve. The scoring engine's own template is
  excluded — it's looked up by `TF_VAR_template_vm_id`, even though it may carry the same tag.
- **`destroy_bridge_if_exists()`** — only HTTP 404 means "no such bridge"; every other error must
  warn (a swallowed failure leaves a stale bridge behind).
- **`_prompt_int()` / `_prompt_difficulty()` / `_prompt_optional_int()`** — re-prompt instead of
  crash on non-numeric input; blank takes the default, and `_prompt_optional_int`'s blank means
  `None` (no value), which is meaningful for `disk_gb`.
- **`collect_teams()`** — `identifier = 100 + i`, so `team1` → `101` (`192.168.101.0/24`).
- **`collect_boxes()`** — box set is per-competition, not global (different events get different
  boxes); it's asked fresh at creation and a reused competition replays its saved `boxes.json`.
  Template selection accepts either an index or a template-name string matching
  `list_proxmox_templates()` exactly — the name path removes the fragile index-only selection
  (index still works). **Blank `disk_gb` keeps the template's own disk**: Terraform only emits a
  disk block when it's set, because Proxmox cannot shrink a disk — a value below the template's
  own size fails the clone.
- **`collect_users_config()`** — credlist input must be exactly 3 names, else it falls back to the
  defaults; always returns values so `users.json` can be written for pinning.
- **`load_injects()` / `resolve_inject_times()`** — inject offsets are minutes relative to
  competition start; they are resolved to RFC3339 timestamps at creation time (phase 7, right
  before `create_injects`) so they anchor to the actual start, not to deploy start.
  Consequence: a `redeploy-competition.py --mode rollback-ready` reset does NOT reopen them —
  offsets stay anchored at the original phase-7 time (winad-scrim2: all injects closed before
  T0). Add `--reset-event` to re-clone the engine from its template (fresh DB) and re-run
  phase 7, re-anchoring injects at now; `verify` WARNs on any inject already past close.
- **`load_boxes()`** — reusing a competition replays its saved `boxes.json`, not anything derived
  from current `.env`.

## nakon_ops.py

Nakon CLI invocation: config generation, bundle building, and deploy over SSH to the engine.
Nakon is a subprocess with `cwd = NAKON_DIR`, never an import (see Conventions).

- **`os_to_platform()`** — classifies a free-text template name exactly the way nakon does:
  `"windows"` if it contains `"win"` (case-insensitive). `redeploy`'s `box_platform()` goes
  through this so platform classification can't disagree with nakon's routing.
- **`_nakon_randomize()`** — runs `nakon randomize --platform … --services N --vulns N --exclude
  … --source auto --json` with `cwd=NAKON_DIR` for catalog access. On failure the error message
  names the actual prerequisites: the vulndb (MySQL + vulndb-ui) must be reachable from this
  machine, or `VULNDB_UI_URL` set; check `vendor/nakon/.env`.
- **`generate_nakon_config()`** — deterministic re-runs: if either pinned file
  (`box_services.json` / `box_vulns.json`) exists it is honored, and **both files are always
  (re)written** so later unconditional reads don't fail on a half-pinned set. Otherwise there is
  **one randomization per box type** — every team gets identical services, which is what makes
  Quotient's wildcard-IP checks (`192.168._.<octet>`) valid. Budgets: `ceil(difficulty/3)`
  services (min 1), `difficulty` vulns (min 1). **Disruptive vulns are sorted last** (entries may
  be a string or `{"name": …, "vars": …}` — normalized for the sort): they break DNS/apt, so
  package installs must run before they land. The machine list is written per competition
  (`nakon-config.json`), not into shared nakon state.
- **`generate_stage_configs()`** — the M3.2 split of `nakon-config.json` into the golden
  pass and the two post-clone passes (four files: `.nakon-golden/-repair/-final/-postclone.json`).
  `.nakon-golden.json`: ONE machine per box type (team1's copy of the full configuration
  list minus the post-clone subset) placed at the golden IP (`192.168.<team1 id>.<240+i>`);
  repair/final carry every team machine with only that stage's subset, and the combined
  postclone view (`repair ∪ final`) feeds redeploy's convergence sweeps. `unbooted` box
  types (DCs) get NO golden machine — everything that would have ridden their golden
  plants per team in the **repair stage** instead (still pre-domain). Machines whose
  subset for a stage is empty are dropped from that stage's file. Both post-clone files
  carry `box_password` (same gitignored-secret class as `nakon-config.json`).
- **`build_nakon_bundle()`** — `nakon build --config <abs> --out bundles --json`;
  content-addressed under `vendor/nakon/bundles/`, cached when the catalog is unchanged; runs with
  `cwd=NAKON_DIR` for vulndb creds (same error-message contract as `_nakon_randomize`). The
  subprocess is serialized by a module lock: concurrent domain passes (and the golden +
  post-clone builds) build one at a time operator-side while their deploys run in parallel.
- **`run_nakon()`** — scp's the nakon package, bundle, and config to a per-run
  `/tmp/nakon-<tag>` on the engine, stages into per-run `/opt/nakon/<tag>/`, then runs
  `nakon deploy` from there. **Staging is per-run (M2.3)**: the old shared
  `rm -rf /opt/nakon/*` slot was what concurrent nakon runs raced on (the incident behind
  the engine lock's deploy-owner check); each run now creates and removes only its own
  dirs (`finally`), the timeout `pkill` is scoped to the run's staging path
  (`pkill -9 -f '/opt/nakon/<tag>'` — the remote cmdline carries it), and the per-deploy
  `pip3 install paramiko` became check-then-install (`sudo python3 -c 'import paramiko'`
  first) so concurrent runs don't race the engine's pip. The global
  `/opt/nakon/.deploy-owner` marker remains purely as a cross-HOST guard: same-host runs
  share a holder identity and pass.
  `only=` scopes to machine names **without changing
  bundle content**; `strict=True` passes `--strict` so failures abort instead of just reporting
  live. `jobs > 1` passes nakon's `--jobs` through (default via the `nakon_jobs` Compfile knob,
  4): per-machine work is atomic in nakon's runner, so machines plant concurrently while each
  machine's step order — disruptive configs last — is preserved. The concurrency ceiling is
  engine egress/CPU and mirror throughput (see `apt_cache` below), never the datastore: plants
  do no bulk storage writes. Redeploy deliberately keeps `jobs=1` — a mid-event repair sweep
  stays maximally gentle on live boxes.
- **`_run_single_nakon_config()`** — deploys one machine with an overridden configuration list in
  isolation (own temp config, own bundle, own `--only`), which is reboot-safe; used by the domain
  pass where each step (`ADDS`, `Domain Join`) reboots the box and would truncate a combined plan.
- **`is_windows_template()`** — backwards-compat alias over `_is_windows_template` (some callers
  import it from here).

## engine_ops.py

Scoring-engine bootstrap (Docker, Quotient), NAT/firewall durability, and the `event.conf` push.

- **`bootstrap_scoring_engine()`** — `postgres_password` / `redis_password` are generated once per
  `deploy()` run and passed in so the Quotient stack `.env` written here **agrees** with the one
  `push_event_conf()` writes later (Postgres and the app must see the same values). Also: apt
  locks are cleared first (`killall apt-get apt dpkg`, lock removal, `dpkg --configure -a`) to
  survive an interrupted first boot.
- **apt-cacher-ng (M1.3)** — installed and enabled during engine bootstrap; it listens on 3142 on
  all interfaces with zero config. `prep_apt_on_boxes(use_proxy=…)` writes
  `/etc/apt/apt.conf.d/95tz-proxy` pointing each box at its own gateway IP
  (`192.168.<id>.1:3142` = the engine's team NIC), gated by the Compfile `apt_cache` knob
  (default on); `use_proxy=False` actively *removes* the file so toggling the flag between
  deploys leaves no stale proxy. Packages cross the mirror once per competition and the LAN
  after — that's what lets `nakon_jobs` scale past mirror throttling. Approved trade-off: the
  proxy is an IP, so apt no longer needs box DNS — `resolv-conf-null-dns` still breaks name
  resolution for everything else on the box but no longer breaks apt; `apt-sources-empty` and
  the hold configs break apt either way.
- **`docker compose up` timeout 600** — the first up after a `--no-cache` build cold-starts
  postgres (initdb) and recreates every container; measured >60 s twice on e2e-2026-09-19.
- **Post-Docker iptables restore** — Docker sets the `FORWARD` policy to `DROP` and wipes custom
  rules on start; forwarding + the team-to-team DROP rule are re-asserted immediately. The same
  pass sets `AllowTcpForwarding yes` in `sshd_config` and reloads sshd — the `ProxyCommand -W`
  gateway tunnels need it.
- **`range-firewall.service` / `.timer`** — `After=docker.service`, `OnBootSec=30`,
  `OnUnitActiveSec=30`: re-asserts team NAT (MASQUERADE) + team-to-team isolation (DROP) every
  30 s for the life of the range, because Docker wipes iptables on any restart.
- **`install_range_healthcheck()`** — 60 s timer logging failures only to
  `/var/log/range-healthcheck.log`. The API probe is deliberately **not** `curl -f`:
  `/api/login` is a POST-only route, so a plain GET correctly gets 405 — that's still proof the
  server is up; only curl's `000` (no connection at all) counts as down. The timer's
  `OnBootSec=60`/`OnUnitActiveSec=60` is offset from `range-firewall.timer`'s 30 s (`:00`/`:30`)
  cadence so the two don't fire in the same tick under systemd's default randomization-free
  scheduling. Checks: Quotient containers running, API responding, isolation DROP rule present,
  NAT MASQUERADE present.
- **`ensure_nat_forwarding()`** — the full story: the engine is every team's NAT gateway (nakon
  needs apt-get), but Docker re-syncs iptables on any container start/restart and drops the
  team-subnet MASQUERADE, leaving boxes offline — and nakon swallows the resulting apt failures,
  so services silently don't install. The same resync drops the team-to-team DROP rule.
  `range-firewall.timer` re-asserts both every 30 s once live, but isn't installed until later in
  `bootstrap_scoring_engine()` — this call covers the gap during phases 4–6, before nakon runs.
  Call it right before any step that needs the team boxes online (i.e. before each nakon run).
  Only the boxes→internet path needs NAT; engine→box scoring is direct routing on the team
  bridge. Idempotent by construction (`-C … || -I/-A`).
- **`push_event_conf()`** — takes the same per-run secrets `deploy()` generated: the passwords
  passed to `bootstrap_scoring_engine()` (so the `.env` rewritten here matches the one it wrote)
  and the `box_creds` passed to `fix_services_on_boxes()` (which creates those OS accounts on the
  boxes). **`box_creds` is what Quotient's Ssh/Smtp/Imap/Sql/Ftp credlist checks authenticate
  with — the names must equal `fix_services_on_boxes()`' accounts, and there are deliberately no
  defaults**: any mismatch or stale fallback literal silently scores healthy boxes as down. The
  credlist lands at `/opt/quotient/config/credlists/linux.credlist`; Quotient is restarted
  afterward to pick up the new config.
- Team bridge NICs (ens19/ens20/…) need no configuration here — Terraform's
  `null_resource.team_nics` + `null_resource.reboot_scoring_engine` have fully addressed them by
  the time this phase runs.

## windows_ops.py

Windows provisioning over the QEMU guest agent — the only channel before networking or
credentials exist.

- **`bootstrap_windows_box()`** — waits for the guest agent first and **raises on timeout**,
  because every downstream step depends on it (unlike the polling wait helpers). The embedded
  PowerShell configures the single `Up` adapter (static IP/prefix 24/gateway/DNS), sets the
  `Administrator` password via `net user`, enables `sshd` + `QEMU-GA`, and creates the port-22
  firewall rule. **The password set here is load-bearing**: nakon's paramiko connection
  authenticates with it (see nakon/deploy/ssh.py) — without this the account still has whatever
  the template baked in, which nothing downstream knows.
- **`dns_repoint_windows_box()`** — points Windows DNS at the team's DC before a domain join:
  `Add-Computer` needs the SRV records from an authoritative server. Guest-agent equivalent of the
  Linux DNS repoint.
- **`wait_for_windows_sshd()`** — polls `Get-Service sshd` via the agent: agent-up does **not**
  imply sshd-up after the ADDS reboot. Never raises.
- **`wait_for_dc_dns()`** — polls the DC for the `_ldap._tcp.dc._msdcs.<domain>` SRV record; DNS
  may lag the guest agent coming back. Never raises.

## hardening_ops.py

Post-nakon hardening and DNS/auth fixes over the gateway, with guest-agent fallback.

- **`wait_boxes_settled()` / the old 150s sleep** — the sleep waited out the post-boot
  unattended-upgrades storm that starves both the guest agent and the ssh forward; the probe
  polls the actual condition per box instead (agent ping + no `unattended-upgr` process +
  dpkg lock free via `apt-get check` with a 1s lock timeout — no psmisc dependency). Ceiling
  240s; a box that never settles falls through to prep's own retry ladder, same as before.
  Runs once per `prep_apt_on_boxes` call (twice per deploy).
- **`fix_dns_on_boxes()`** — 8 attempts, 15 s apart, per box; after the last, falls back to
  `guest_agent_exec_root` with `DNS_FIX_CMD_ROOT`, which needs neither working sudo (NOPASSWD may
  not be granted yet on a fresh clone) nor the network the SSH path uses (it runs as root over
  virtio-serial). **All-fail aborts**: if every box failed, this looks systemic (see the
  guest-agent diagnosis per box), not a timing fluke — abort rather than proceed into nakon
  against boxes already known unreachable. Individual failures warn and continue.
- **`fix_services_on_boxes()`** — `box_creds` is required, no fallback: it's the same per-run
  secret `deploy()` passed to `push_event_conf()`; a fallback literal here would recreate the
  accounts with passwords Quotient's credlist checks don't know, scoring healthy boxes as down.
  Per-box script (base64-encoded to avoid ALL quoting issues), always:
  - creates the credlist OS accounts (`useradd` + `chpasswd`) — every auth-based check needs them;
  - **un-wedges sshd**: several catalog configs (`ssh-root-login`, `ssh-empty-passwords`,
    `ssh-password-auth`, `ssh-max-auth-retries-high`, `ssh-x11-forwarding`, …) each independently
    run `systemctl restart ssh`; landing several back-to-back with no delay trips systemd's
    crash-loop protection (`Result: start-limit-hit`) and leaves sshd down for the rest of the
    competition — confirmed live 2026-09-03 (web01-team101, 5 ssh-* configs, journalctl showed 5
    restarts inside one second; see docs/known-issues.md). `reset-failed` + conditional `start`
    reaches the box over SSH while it's still up and via the guest-agent fallback when it isn't —
    either way sshd ends the pass running.
  - per service: MySQL/MariaDB bound to `0.0.0.0` plus credlist DB users (`CREATE USER … @'%'` +
    `GRANT ALL`, first account also `WITH GRANT OPTION`); Postfix `inet_interfaces = all` +
    re-adding the `smtp/inet` master.cf entry (non-interactive installs can leave it empty);
    vsftpd anonymous off / local+write enable; nginx restart; **Dovecot** plaintext auth on, with
    the 2.4+ key (`auth_allow_cleartext = yes` in a `99-` conf) added **only** on Dovecot ≥ 2.4
    (version-gated — 2.4 renamed the key), plus a mail dir per credlist account; **bind9**
    `allow-query { any; }` + forwarders, recreating `named.conf.default-zones` if missing and
    **`db.local` if the template stripped it**; telnet via `update-inetd --enable`; **Splunk**:
    nakon only creates a user, so lighttpd is installed and moved to port 8000 to have something
    listening (Quotient's splunk check expects port 8000); finally a sweep that starts every
    installed service.
  - when the SSH run exits non-zero — **writable-sudoers-type misconfigs break sudo** — the same
    script is retried via the guest agent as root, with `sudo ` stripped by
    `re.sub(r"\bsudo ", …)`: `\b`, not `^\s*`, so mid-pipeline uses like `echo … | sudo chpasswd`
    are caught too, not just line-leading ones.
- **`setup_ubuntu_auth()`** — enables sshd password auth + NOPASSWD sudo for `box_username`
  (nakon authenticates by password and runs sudo). 8 attempts, then the same guest-agent fallback
  as root: planted misconfigs (writable-sudoers et al) break sudo for the SSH path, and the
  fallback beats burning 2 minutes of retries and moving on.

## golden_ops.py

Golden-set build and template conversion (M3.1). One VM per box type is full-cloned from
the base templates to `engine_vmid + 150 + box_idx`, planted with the **golden stage**
(strict, tz-base rollback guard on re-entry), cloud-init-cleaned, stopped, and converted
with `qm template`. Terraform apply #2 then link-clones every team from those templates.

- **Why the API and not Terraform** — `qm template` conversion happens outside the VM
  resource's lifecycle, so a Terraform-managed golden box would drift the moment it
  converts. Terraform only consumes the finished templates as `clone { full = false }`
  sources via the `golden_template_ids` tfvars map that `deploy()` persists.
- **Golden placement** — IPs sit at `192.168.<team1 id>.<240+i>` on team1's bridge: above
  the `.1` gateway, below `.255`, and free because golden boxes are converted to stopped
  templates before any real team box exists. The vmid block is gated by preflight
  (collisions with team space fail fast with a pointer to `--scoring-vmid`).
- **Resume routing** — all golden vmids already templates → skip; some exist as plain VMs
  → roll the tz-base-marked ones back and re-plant, clone the missing; a *partially
  converted* set is fatal (templates can't be un-templated) — destroy and redeploy.
  M4 exception: partial conversion is fine when every converted vmid's description hash
  matches its expected hash (selective rebuild / added box type). Only **unconverted
  slots** are worked on — a selective rebuild never starts, plants, or re-converts the
  matching templates it keeps. Cold (unbooted DC) slots: any plain VM is a dead attempt
  that may have booted and specialized, so it is destroyed and FULL-cloned from the
  generalized base, then converted to a template **without ever booting** (no
  start/bootstrap/snapshot/plant).
- **`unbooted_golden_boxes()`** — absent `domain_roles.json` = no-domain lineup (empty
  set). A present-but-invalid file (unparseable, non-map, role value outside
  `dc`/`member`, or a name absent from `boxes.json`) raises rather than silently
  widening the booted set — the silent fallback would hand the DC a booted shared
  golden, the exact duplicate-DomainSID failure the unbooted split exists to prevent.
- **Windows identity (plan 3.0)** — linked clones see the same bytes a full clone would;
  SIDs already duplicate across teams today (only the original templates are sysprepped)
  and stay unique within a team. Live-checked 2026-09-24: phase 6 never re-sysprepped.

## template_ops.py (M4)

Per-competition template lifecycle: `build → test runs (reuse; rebuild on config change)
→ freeze → competition → destroy`. NOT a cross-competition cache — every template belongs
to exactly one competition, dies at `--full` teardown, and is never patched in place.

- **Hashes** — sha256 per template over only what affects disk contents; every input is
  tagged **config-class** (base template vmid, golden-stage config list incl. pinned
  `{name, vars}` values, the PER-BOX `payload_hash` — that box's golden plan's step
  `script_sha256`s, content-addressed, so a web01 pin change does not rebuild
  app01/dc01/win01 (matrix run 4: one change rebuilt all four) — box user/password,
  SSH key, apt-proxy flag; engine: base image vmid + pinned `quotient_ref`) vs
  **code-class** (the disk-affecting function sources — engine: bootstrap + clean step —
  plus the engine's `scoring_engine` main.tf resource block only, so team-box edits
  don't rebuild the engine). Frozen records written before the payload-hash migration
  carried the whole-bundle `bundle_id`; `golden_freeze_gate` honors those only while
  the bundle is still unchanged — a changed bundle enters the normal hard-fail path.
  Excluded, with
  reasons recorded in the module docstring: team count/identifiers/IPs, event.conf,
  repair/final-stage configs, snapshot names, mgmt IPs, DB passwords (all applied after
  cloning or cleaned from the template). Going 2-team test → 8-team competition rebuilds
  nothing. Hashes live on the template **description** (node-side truth) and in
  `.template-hashes.json` (survives `.deploy_state.json` resets; 0600, gitignored —
  golden inputs embed box_password).
- **Engine template** — built API-side like the goldens (terraform only consumes it as
  `engine_clone_id`): full clone of the base image → bootstrap (pinned Quotient ref —
  the hash input must be computable BEFORE building; the realized HEAD is recorded for
  traceability only) → clean step (compose down -v, remove `.env`/event.conf/credlists,
  truncate machine-id, cloud-init clean, remove SSH host keys; apt-cacher-ng stays installed
  but its cache is empty at build time — the warm cache is built on the deployed clone and
  dies with it, so the template gives no apt speedup) → `qm template`. The deployed engine is a linked clone with a
  fresh identity and fresh host keys every run, and `.env` goes down BEFORE `compose up`
  so the fresh postgres volume initializes with this competition's credentials.
- **Reuse vs rebuild** — hash match → reuse (logged); differs + not frozen → destroy
  that template's clones, destroy the template, rebuild; differs + frozen → see freeze
  semantics; missing → build. Golden rebuild granularity is per box type (changing one
  box's golden-stage pin rebuilds only that box's golden).
- **Frozen semantics** — a frozen competition never rebuilds: **config-class drift
  hard-fails naming the changed fields** (the event runs on the hashes that were
  verified); **code-class drift warns and proceeds off the frozen template** — a
  post-freeze log line must never break a mid-event team rebuild or engine recovery.
  This holds fully for the ENGINE (the phase-2 gate runs before any engine destruction
  and reuses the frozen template). Known gap, live-found 2026-09-26 (scenario-7 leg 2):
  phase-1's golden keep/destroy compares record hash vs freshly computed hash WITHOUT
  consulting the frozen gate, so a code-only drift of a golden's inputs (e.g. a
  `build_golden_set` source change) destroys and rebuilds the affected goldens instead
  of proceeding on the frozen template — the top-of-deploy warning fires, then phase 1
  overrules it. Config drift still hard-refuses; fixing the golden side means letting
  phase 1 ask the gate before destroying.
  Mid-event rebuilds and recoveries also warn (not block) when the node's template hash
  differs from the verified record.
- **Freeze** — `verify-competition.py --freeze`: requires all gates PASS including the
  plant-coverage gate, records hashes + git commit + dirty flag + timestamp + gate
  results in `.frozen.json` (0600). Windows/domain lineups additionally require
  `--windows-domain-validated` (the operator's attestation that the run exercised
  DomainSID uniqueness, machine SIDs, and the three-pass ordering). `--unfreeze
  --confirm-unfreeze` removes the record — pre-competition use only.
- **Teardown modes** — destroy-competition defaults to **teams-only** (clones + engine
  VM + bridges; templates kept for hash reuse); `--full` also destroys templates
  (clones strictly first — linked clones die with their base disks); `--full` on a
  FROZEN competition refuses without `--end-of-competition`. A full teardown also
  removes `.template-hashes.json` — a surviving record would make the next deploy
  "reuse" a hash with nothing behind it.
- **Team rebuild** — redeploy `--mode rebuild` re-clones from the frozen goldens and
  runs the POST-CLONE STAGES in deploy order (repair → domains → final), never the
  full config — planting the disruptive/boot-hostile stage before the domain joins is
  exactly the brick hazard the three-pass split removed. Timed to
  `.deploy-timings.jsonl`.
- **Engine recovery** — redeploy `--mode engine-recovery`: `terraform apply -replace`
  on the engine resource only, re-cloned from the engine template, per-deploy state
  reapplied (fresh empty scoring DB); re-seed with `--from-phase 7`.
- **REQUIRED_VARS + bundle lint** — the catalog DB has no required-vars metadata, so
  constants.py curates the table (`"ip"` = machine-identity: auto-filled per machine at
  stage generation and banned from the golden stage — a golden-baked identity var would
  clone the golden's IP into every team; `"literal"` = must be pinned as
  `{"name", "vars"}`). Every built bundle is linted: payload blobs are scanned for
  `$VAR` references that the step doesn't declare and the script doesn't
  assign/guard/self-default — a catalog config that starts needing an undeclared var
  fails at bundle build, before any plant time is spent (validated against all 123
  existing bundles: zero false positives; the one real hit class is genuinely broken
  bare pins like `install-package` without `PACKAGE`).

## clone_ops.py

Full-clone of team1's boxes to team2+ over the Proxmox API, plus re-IP/bridge, ordering, and
bookkeeping.

- **`_agent_ipv4_present()`** — guest-agent exceptions count as "not yet", never as "missing": a
  box whose agent is still booting must not be "repaired" while cloud-init is merely slow.
- **`_repair_box_network()`** — the e2e-2026-09-19 lesson (app01; see docs/known-issues.md):
  clones can come up without the `ipconfig0` address (cloud-init race), and ifupdown loses the
  address on any carrier blip. Repair = re-add the address + default route live via the guest
  agent (root; no network or sudo needed), then **persist** it: a systemd-networkd `.network` with
  `KeepConfiguration=static`/`dhcp` (keeps the address through carrier/link churn) and, for
  ifupdown systems, a static `interfaces.d` stanza. Both apply the same address, so coexisting is
  harmless.
- **`ensure_cloned_network()`** — post-start IPv4 check + repair for every Linux box, run right
  after the start loop and before the SSH waits: a clone without a routable address would
  otherwise fail phase 6/7 an hour later. Windows boxes are excluded —
  `bootstrap_windows_box()` does its own network configuration.
- **`clone_team_boxes()`** ordering and details:
  - `cloud-init clean --logs --machine-id` on team1 Linux first (Windows uses sysprep instead):
    without it, clones inherit team1's machine-id/cloud-init state (duplicate-identity bugs).
  - **0-based box index feeds `vm_id_for()`** (Terraform creates VMs with a 0-based index) while
    `last_octet` feeds IP addresses — the two are different counters.
  - Cloned VMIDs are tracked in `competitions/<id>/cloned_vms.json` for destroy + resume — they
    are **not in Terraform state**. An existing destination vmid means "already cloned" on resume
    (clone is skipped).
  - Windows clones get only `net0` reconfigured; Linux clones get `ipconfig0` + `net0`.
  - Step 4.5 (`ensure_cloned_network`) runs before any SSH wait, per above.
  - **`setup_ubuntu_auth()` runs BEFORE `fix_dns_on_boxes()`**: on fresh clones the DNS fix's
    sudo soft-fails 8× per box until the sudoers grant lands; the guest-agent fallback in
    `fix_dns_on_boxes()` covers any remaining gap.
  - Cloned team2+ boxes are snapshotted `tz-base` before phase-6 nakon (team1 already got its
    `tz-base` in phase 5).

## domain_ops.py

Per-team AD forests: DC promotion and member joins, run after cloning. Driven by
`domain_roles.json`; no-op when it's absent.

- **`_probe_joined()`** — live membership probe via the guest agent ("root truth", no SSH
  needed): `Win32_ComputerSystem.PartOfDomain` on Windows, `realm list` on Linux. **Joins are not
  idempotent** (`realm join` / `Add-Computer` on an already-joined member fails, and the
  single-config pass runs strict), so resumes must skip members that are already in the domain.
- **`deploy_domain_configs()`** — runs after clone to avoid DC clone duplication; each rebooting
  step runs in its own isolated nakon pass (`_run_single_nakon_config`). Teams run
  **concurrently** (M2.4) — each team's chain is serial within itself (promotion must precede
  joins) — safe only since M2.3 gave every nakon pass its own engine staging dir.
  `promote_dc=False` only
  rejoins members (the redeploy case).
  - **ADDS resume artifact** (`.nakon-domain-<team>-adds.json`): its existence means ADDS already
    ran for this team — re-running `Install-ADDSForest` on a live DC would just fail. Promotion
    is skipped on resume; the joins below still run (they're the recoverable part).
  - **AD DS promotion budget: up to 20 min** (`wait_for_guest_agent` timeout 1200) — promotion is
    slow and ends in a reboot.
  - **AD Web Services readiness precedes AD-aware plants**: after ADDS reboots, `sshd` can
  return before the AD PowerShell cmdlets are ready. `wait_for_adws` polls `Get-ADDomain`
  (and records the DomainSID) before `Add User Account` / `Elevate User Account` are planted.
  The AD request order remains Add, Elevate, firewall, auditing; nakon's pinned dependency
  handling emits one parameterized Add step rather than a duplicate vars-less Add.
  - **AD misconfigs run with `strict=False`**: this pass is scoring flavor, not range
  infrastructure, and can fail non-fatally even with nakon ≥ v0.1.3 (which fixed the duplicate
  vars-less dependency step): "Disable System Firewall" sweeps every AD computer over WinRM, and
  a Linux realmd member has no WinRM, so that step exits 1 on any mixed Windows/Linux domain
  after landing its own misconfig. Failures print in nakon's summary instead of aborting.
  - DC DNS SRV records are awaited (`wait_for_dc_dns`) before any member join; join membership
  is then probed and transient join failures are retried up to three times.

  - **Windows members** get DNS repointed at the DC first, then `Domain Join` (reboots);
    **Linux members** join via nakon's `domain-join` (realmd/sssd) — no reboot and no separate
    DNS repoint needed.
  - **Linux member join failures are scenario flavor, not infrastructure**: the box keeps local
    auth and every scored service. The apt install of the realmd stack can blow past nakon's
    per-step timeout on small VMs — log loudly and keep deploying (hence `strict=False` + catch).

## timing.py

Deploy timing instrumentation (M0.1). `timed(comp_dir, phase, op, target)` is a context
manager appending one JSONL line per operation to `competitions/<id>/.deploy-timings.jsonl`
(gitignored, no secrets — op names and durations only). Phases wrap their big-ticket ops
(terraform apply, engine bootstrap, per-VM clone/start/destroy/snapshot, waits, apt prep,
nakon passes with machine counts, ADDS/joins, seed) in `deploy.py`, `clone_ops.py`, and
`domain_ops.py`. Appends are lock-serialized, so M2's concurrent workers can write the same
file safely. `print_timing_summary` totals seconds per (phase, op) at deploy end. The
baseline comes from a real competition deploy, not a synthetic one.

## deploy.py

The seven-phase orchestrator and CLI. `deploy()` owns phase sequencing, resume, and credential
generation.

- **Pipeline v2 (M3) phase map, M4-revised** — [1] two-wave parallel cleanup (team boxes
  + legacy clones, then engine + hash-mismatched/missing goldens — **matching golden
  templates survive**: test-run reuse) · [2] engine-template lifecycle (hash → reuse or
  rebuild; never on a frozen competition) + terraform apply #1 (engine as a *linked
  clone of the engine template* + bridges, `build_team_boxes=false`) · [3]
  prepare-engine-from-template (`.env` BEFORE `compose up`, fresh volumes ⇒ empty
  scoring DB) + `event.conf` · [4] golden hash gate + golden build/convert (per-box
  hash onto the template description) + terraform apply #2 (all teams as linked clones)
  + waits/auth/DNS/apt/`tz-base` · [5] repair-stage sweep (sshd/sudoers, lenient,
  `--jobs`) + `fix_services_on_boxes` (AFTER the sweep: it un-wedges the sshd the ssh-\*
  configs just thrashed) · [6] domains (concurrent per team) → **final-stage pass**
  (disruptive + boot-hostile, planted after the domain reboots) → beacons + `tz-ready`
  · [7] seed. `.phase6-swept` became `.postclone-swept`. A state file with a different
  `pipeline_version` refuses to resume — the phase numbers mean different things.
- **The full nakon bundle is no longer built or deployed** — `generate_stage_configs`
  splits `nakon-config.json` into `.nakon-golden.json` (one machine per box type at the
  golden IP, golden-stage subset), `.nakon-repair.json` and `.nakon-final.json` (every
  team machine, that stage's subset — the repair/final split exists because domain
  joins reboot boxes and need DNS/apt; see constants.py), plus the combined
  `.nakon-postclone.json` view for redeploy sweeps. All four are 0600 and gitignored
  (they carry box_password). Only the stage bundles are built, content-addressed as
  ever, and each is linted for undeclared `$VAR` references at build time.

- **Resume semantics (`from_phase > 1`)** — skips the destructive [1/7] cleanup and [2/7]
  terraform apply, and reloads teams + per-run secrets from
  `competitions/<id>/.deploy_state.json` so the resume agrees with what was already deployed.
  After each numbered phase, the last-completed number is checkpointed to that file; on failure
  the exact resume command is printed.
- **Resume-must-reuse-original-secrets invariant, and the pre-guard** — `--from-phase N` with no
  `.deploy_state.json` is a hard `SystemExit`. Without this guard, the fresh-deploy branch ran as
  if from scratch — minting NEW passwords and overwriting `teams.json` — while `from_phase` still
  skipped the destructive phases 1–2, leaving the deployed range and its credentials silently out
  of sync (e.g. nakon authenticating with a password no box has).
- **Secret-regeneration persistence** — when resuming with an older state file that predates a
  given secret field, the missing secret is regenerated once and immediately written back into the
  state file, so a *second* resume reuses the same value instead of minting yet another one that
  disagrees with what's already on the engine/boxes.
- **Per-competition generated passwords** — `admin` / `inject` / `postgres` / `redis` /
  `box_password` / `box_creds` are generated fresh per run, replacing the fixed `ubuntu/ubuntu` +
  `admin/changeme123` literals this used to ship with: a fixed value across every deployment is
  guessable from this open-source repo, or from fingerprinting a past deploy. Postgres/Redis are
  generated once and passed to both `bootstrap_scoring_engine()` and `push_event_conf()` so the
  two `.env` writes agree.
- **`KNOWN_BROKEN_TEMPLATES` warn-don't-block** — templates `ubuntu24.04` (vmid 106) and
  `debian13-lite` (vmid 920) have bad cloud-init; clones never get a working network/SSH (use the
  `-fix` variants). The warning is unconditional — not gated by `--yes`/`confirm_deploy` —
  because a reused competition replays its saved `boxes.json` exactly and can silently outlive
  the interactive box-picker warning, and a non-interactive/CI deploy skips that prompt anyway.
- **Stage bundles built operator-side** — the nakon bundles are built on the operator machine:
  the only steps that need the vulndb (MySQL + vulndb-ui/MinIO), which `generate_nakon_config()`
  has already proven reachable. Pipeline v2 builds three stage bundles (golden + repair + final,
  via `generate_stage_configs`), not one full bundle, so **the scoring engine never sees vulndb
  credentials** and no full-machine pass is ever deployed.
- **`teams.json` persisted** — `destroy-competition.py` requires it to tear the competition down
  later; verification also reads team logins from it instead of re-deriving.
- **`all_targets` built once from the FULL box list** — see `enumerate_targets()`'s invariant:
  nothing downstream may re-derive a vmid from a filtered `boxes` (vmid is positional).
- **`confirm_deploy()` skipped on resume** — resuming implies prior confirmation; `--yes` skips it
  outright on a fresh deploy.
- **Sweep marker reset** — a fresh deploy unlinks `.postclone-swept` so it never inherits a
  previous deploy's marker.
- **[2/7] terraform apply #1** — `terraform apply -parallelism=1` with `build_team_boxes=false`:
  the engine (full clone from the engine template) + all team bridges + the engine's team NICs
  (netplan) + the cold boot. `parallelism=1` because concurrent full clones saturate the
  datastore/API (HTTP 596); the engine's SSH reachability is **polled** (`wait_for_ssh`) instead
  of a blind post-apply sleep.
- **[3/7] engine bootstrap** — the old phase 3 (remote SSH-key copy) was removed as unused, and
  v2 moved the engine bootstrap into its slot: the golden plant in phase 4 is a nakon run, so
  the engine, `event.conf`, and NAT must all be live first (pushing `event.conf` before any
  nakon run prevents the Quotient crash loop that wipes NAT).
- **[4/7] golden build + apply #2** — `build_golden_set` (API clones at `engine+150+i`, golden
  plant strict under the tz-base rollback guard, `cloud-init clean`, `qm template`), then
  `build_team_boxes=true` and the second apply: every team's boxes as **linked clones** of the
  golden templates. Windows bootstrap then covers ALL teams (guest agent is the only pre-network
  channel; no cloud-init on Windows), followed by concurrent waits, `setup_ubuntu_auth`,
  DNS fix, apt prep, and `tz-base` for every box. Clones boot with working DNS/apt because the
  golden disk carries no disruptive configs — the pre-plant repair stage the v1 pipeline needed
  after cloning is gone.
- **[5/7] post-clone sweep** — nakon pass 2 over the post-clone stage (`.nakon-postclone.json`),
  lenient (`strict=False`: one flaky plant must not kill the sweep after 98% landed), `--jobs N`,
  gated by `.postclone-swept` so resumes skip it. `fix_services_on_boxes` runs **after** the
  sweep, not before: the ssh-* configs in the post-clone set restart sshd and can trip the
  start-limit, and the un-wedge + credlist accounts + service binds belong after the thing that
  breaks them. (Historical note: through 2026-09-24 the equivalent phase-6 sweep passed no
  `only=` at all — the monolith's comment said "team1 already done at [5/7]" but the filter was
  never implemented, so team1 was re-planted over its finished strict plant on every deploy.)
- **[6/7] domains + beacons + tz-ready** — `deploy_domain_configs` runs teams **concurrently**
  (each team's ADDS→join chain serial within itself); team beacons (if `team_beacons 1`) are
  planted **before** the `tz-ready` snapshot so restore points carry them.
- **[7/7]** — Quotient's HTTP is polled (`wait_for_http` on `/api/login`) instead of a blind sleep
  before seeding. Each sub-step is gated on its own state flag (`seeded`, `engine_unpaused`,
  `injects_created`) because **`unpause_engine` is not idempotent**. `resolve_inject_times()` runs
  here so offsets anchor to the actual competition start, not deploy start.
- **Failure handler** — `current_phase` is tracked so the handler can say exactly where to resume.
  An "already exists" error at phase ≥ 2 suggests a Proxmox/Terraform state mismatch (something a
  prior attempt created still exists, but Terraform's state doesn't know about it), so the
  recommended resume point is **phase 1** — resuming from the failed phase would just hit the
  same error again.
- **`credentials.txt` (mode 0600)** — the durable, non-log record of every secret this run
  generated; the console summary prints them for convenience but the file is the authoritative
  copy, chmod 600 so it isn't world-readable. Includes the `box-login`/`box-credlist-*` lines.
- **`--plan-only`** — collects/generates the competition's config and prints a summary, then exits
  without touching infrastructure. Exists because there is no confirmation checkpoint between the
  box picker and a real deploy otherwise: `--yes` skips it outright, and piped/scripted stdin that
  happens to satisfy every remaining prompt walks straight into a real deploy. Review the plan,
  then re-run without the flag.
- **`main()` paths** — `--competition` reuses an existing competition straight to `deploy()` (no
  stdin) or creates one, falling back to prompting only for missing pieces; boxes are still
  collected interactively (no non-interactive box spec yet). The interactive path passes
  `--teams/--yes/--from-phase` through with None/False/1 defaults, preserving the exact prior
  behavior.

## create-competition.py (shim)

Re-export shim only; all logic lives in the focused modules.

- **Why the shim exists** — keeps `import create-competition` (via importlib) working for
  `redeploy-competition.py`; the hyphen in the filename forces `importlib`, and every historical
  `driver.<symbol>` consumer keeps resolving.
- **`is_windows_template` triple definition** — `nakon_ops`, `ssh_ops`, and `windows_ops` each
  define one; the shim imports the `nakon_ops` one, which is what `driver.is_windows_template`
  (and therefore redeploy's `box_platform`) resolves to. All three must agree ("win" in
  lowercase) — a divergence would make Terraform, nakon routing, and the driver classify the same
  template differently.
- **`ENV_PATH`** — re-exposed as `Path(".env")` for backwards compatibility with the original
  monolith.

## destroy-competition.py

Teardown: API-cloned VMs first, then `terraform destroy`.

- **TF_VAR restoration before destroy** — per-competition `TF_VAR_teams` /
  `TF_VAR_boxes_per_team` / `TF_VAR_event_name` are restored from the saved files so
  `terraform destroy` uses the **exact same resource keys as the original apply**: `for_each`
  over `var.teams` and `var.boxes_per_team` must match, or resources are orphaned.
- **`destroy_cloned_vms()`** — team2+ clones are not in Terraform state, so they're destroyed via
  the Proxmox API (stop, then delete with `purge=1`) before `terraform destroy`; already-stopped
  or already-gone VMs are tolerated silently. (Pipeline v2 keeps this path for legacy ranges;
  v2 ranges have every team in Terraform state and skip it.)
- **Golden wave last (v2)** — after `terraform destroy` removes every team box, engine, and
  bridge, `destroy_golden_set` API-destroys the golden templates (`engine_vmid + 150 + i`), with
  the ownership-tag check (`tezcatlipoca`, `tezcatlipoca-golden`, `comp-<name>`). Clones must
  die before their templates' base disks.
- **`load_destroyable_competitions()`** — requires `Compfile` + `teams.json` + `boxes.json`:
  competitions deployed before `teams.json` support can't be safely destroyed this way.
- `terraform destroy -parallelism=1`, matching the apply.

## redeploy-competition.py

Filtered per-team/box rollback/reconfigure/rebuild against a live range, using snapshots.

- **Modes** — `rollback-ready` (tz-ready, the default; seconds), `rollback-base` (tz-base, then
  re-run the **post-clone stage** + hardening + domain), `reconfigure` (no rollback — run the
  configure chain against the live boxes), `rebuild` (recreate from template). Any rollback
  **discards everything the defending team(s) did to those boxes** — the confirmation prompt
  says so.
- **Pipeline-v2 awareness** — when `.deploy_state.json` says `pipeline_version: 2`, repair
  re-plants (`rollback-base`/`reconfigure`/`rebuild`) run the **post-clone stage**
  (`.nakon-postclone.json`), not the full config: the golden-stage installs ride the linked
  clone, and re-running them over live boxes is exactly what the stage split removed.
  `rebuild` clones from the **golden template** (`state["golden_template_ids"]`, linked, so a
  rebuilt box carries the golden-stage installs), and refuses with an explanation if the
  golden template is gone — rebuilding from the ORIGINAL template would produce a box missing
  every golden-stage install. Pre-golden ranges keep the old full-config behavior. The
  Linux-only executors (`fix_dns_on_boxes`/`setup_ubuntu_auth`/`fix_services_on_boxes` — bash
  against Ubuntu boxes) receive Linux targets only on every path: reconfigure/rollback use the
  same `linux_targets` list deploy does, rebuild filters its `rebuilt` set, while Windows
  boxes keep the guest-agent bootstrap, the domain chain, waits, snapshots, and nakon.
- **`_load_driver()`** — `create-competition.py` has a hyphen, so it's loaded via
  `importlib.util.spec_from_file_location` and re-used as `driver`.
- **`box_platform()`** — routes through `driver.os_to_platform` (must match nakon's
  classification, see `nakon_ops`).
- **Secrets must be the originals** — `box_password` / `box_creds` come from
  `.deploy_state.json`: the credlist accounts this recreates must match what `push_event_conf()`
  wrote to Quotient's `linux.credlist`, or the recovered box scores down on every auth-based
  check even though it's healthy.
- **`mode_reconfigure()` note** — reconfigure never resets disks, so domain membership is assumed
  intact. If the AD domain itself is what's broken, use `--mode rollback-base` (or `rebuild`) —
  those re-promote/re-join after the reset.
- **`mode_rebuild()`** — clones from the **template**, not team1's live box; requires
  `box_password` in state (a rebuilt box must use the login the rest of the range uses).
  **Drift note for team1**: a rebuilt team1 box was recreated outside Terraform, so the next
  `terraform apply` sees drift and wants to replace it — fine mid-event; re-import or accept the
  replacement afterwards.
- **`quote_sshkeys()`** — Proxmox's `sshkeys` config param wants the key URL-encoded.
- **`rerun_domain_configs()`** — re-runs the AD chain only for affected teams;
  `promote_dc` is set only when the DC box itself was reset; without `box_password` in state it
  warns and skips (rejoin by hand, or `create-competition.py --from-phase 6`).
- **`template_vmid_for()`** — resolves a template name to a vmid exactly the way `main.tf`'s
  templates data source does (tagged `template`, excluding the scoring template).
- Snapshot precheck: rollback modes fail fast (with `snapshot_support_hint`) when a selected box
  lacks the required snapshot, before touching anything.

## verify-competition.py

Post-deploy verifier: logins, services, isolation, misconfig spot-check, injects.

- **TLS warnings silenced** — the engine speaks plain HTTP, but Quotient's checks and the Proxmox
  API elsewhere use self-signed TLS; warnings are disabled so output stays readable.
- **Box authentication** — boxes are authenticated with the Proxmox key (cloud-init authorizes it
  for the configured `box_username`); the box password is informational here. The admin password
  is parsed from `credentials.txt`, falling back to `changeme123` (overridable with
  `--admin-password` / `--engine-ip` paths).
- **`MISCONFIG_CHECKS`** — each entry documents what the planted misconfig looks like and how to
  verify it: `suid-find` (the `s` in the owner-exec slot of `ls -l $(which find)`),
  `www-data-shell` (`/etc/passwd` line ends with `/bin/bash`), `bad-perms-userConfig`
  (`/etc/shadow` mode 666), `writable-sudoers` (`/etc/sudoers.d` mode 777 — nakon's
  `004-writable-sudoers.sh`). Only a handful are mapped; the spot-check picks a box/config pair
  it can actually verify.
- **`check_no_default_creds()` false-positive guard** — only the actual value token (last
  whitespace-separated field) is compared against the default literals, not the whole line:
  `box-login (ubuntu)  <password>` legitimately contains the literal `ubuntu` as the non-secret
  username label, which is not a rotation failure.
- **`check_services()`** — a service with no scored rounds yet is "not yet scored", not a
  failure; it's excluded from the UP/DOWN tally. Check `Result` values are normalized (the string
  `"false"`/`"0"` count as failed) since Quotient returns them as strings. Services DOWN is
  reported but not fatal unless `--strict-services`.
- **`check_isolation()`** — the DROP rule match requires **both `-s` and `-d`**
  (`192.168.0.0/16` appears ≥ 2 times), not just one side. With ≥ 2 teams it also runs a live
  cross-team connection test (expected blocked) plus an internet-reachability control; if the
  test can't run, the rule-presence pass above stands and this alone doesn't fail.
- **`check_misconfig_survival()`** — groups are keyed on the **normalized config NAMES** (a tuple
  of `{"name": …}`-extracted strings), not the raw entries: dict-form entries aren't hashable,
  and "the same box, different teams" means the same names regardless of whether one team's copy
  carries `vars`. Presence on some teams but missing on others fails (didn't survive cloning);
  **absent-everywhere is not this check's job** — `check_misconfig()` above already covers "was
  it planted at all".
- **`check_domains()`** — the live replacement for the freeze's operator attestation. Per
  team: the DC answers `Get-ADDomain` for `team<id>.local` **with a syntactically valid
  `S-1-5-21-*` DomainSID** (a promoted DC that answers without one fails — with one team,
  uniqueness alone is vacuous, so the DSID shape check is what actually gates), the planted
  `svc-support` account exists, and every member is joined (Windows `PartOfDomain`+domain,
  Linux realmd). Across teams: duplicate **DomainSIDs** fail (promotion reused image state);
  duplicate member machine SIDs are INFO only (linked clones of one golden, harmless for
  isolated forests). A single-team lineup PASSes with "uniqueness needs a second team".
  Fail-closed on input: a present-but-malformed `domain_roles.json`, a role value outside
  `dc`/`member`, or a role box absent from `boxes.json` FAIL the gate — nothing is silently
  skipped; only file absence skips (no-domain lineup).
- **Exit-code gate** — `isolation` and `misconfig_survival` are NOT optional: a failed isolation
  check means teams can reach each other *right now* (a correctness issue for the whole exercise,
  not a scoring nuisance), and a misconfig missing on one team's clone breaks the "every team
  defends the same misconfigs" fairness guarantee. Down services are soft (informational) unless
  `--strict-services`; logins, the default-creds guard, the misconfig spot-check, and injects (if
  the competition ships any) gate as well. `report_healthcheck_status` / `report_beacons` are
  informational only.

## generate-packet.py

Competitor briefing packet renderer — pure local-file → Markdown, no live infrastructure.

- **Deliberate scope** — the packet is intentionally the SAME document for every team (no
  team-specific data; real per-team credentials are issued separately at competition start via
  `credentials.txt`) and intentionally omits `box_vulns.json` — nakon's planted misconfigs would
  spoil the competition. `box_vulns.json` is deliberately never read here.
- **`_SERVICE_DISPLAY`** — mirrors `quotient/setup.py`'s `_SERVICE_TO_CHECK` Display fields so
  the packet shows the same human-readable service names the Quotient scoreboard does (e.g.
  `apache` → `http`) instead of raw catalog identifiers. Kept as a separate, smaller table rather
  than importing the full dict: this script has no dependency on Quotient's TOML check shapes —
  just the names a competitor would recognize.
- **`load_inject_schedule()`** — titles + timing offsets only, never inject content/description;
  same `inject.json` shape as `config_ops.load_injects()`.
- Works the moment `Compfile`/`boxes.json`/`box_services.json` exist — from a partial
  create-competition run or fully hand-authored (error messages point at the relevant
  docs/usage-agents.md sections).

## quotient/setup.py

Drives Quotient: event.conf generation, team seeding, engine unpause, inject creation.

- **Module split** — `unpause_engine()` is separate from `seed_teams()` because unpausing isn't
  safely repeatable (hence deploy.py's `engine_unpaused` state flag), while seeding is idempotent.
- **`_SERVICE_TO_CHECK`** — maps a nakon service name to its Quotient check key + config dict.
  Keys and field names must match Quotient's Go struct TOML tags exactly (the check-type key on
  `Box` is case-sensitive; field names are matched case-insensitively by BurntSushi). A box
  accumulates multiple checks (apache + bind → Web + Dns); vulns and unrecognized service names
  are skipped with a warning. Per-family rationale:
  - **Web** — `Url` is a required nested array of `{Path, Status}`.
  - **Dns** — `Record` is a required array of `{Kind, Domain, Answer}`.
  - **Ssh** — `CredLists` required for the login check.
  - **Ftp** — authenticated login against `linux.credlist` (the same accounts SMTP scores
    against): the `unauthorized-ftp-server` config installs vsftpd with Ubuntu's default
    `anonymous_enable=NO` / `local_enable=YES`, so an anonymous check can never pass but a local
    login does. Keep box config + check in sync.
  - **Smtp** — `smtp.go` always calls `getCreds`, so `CredLists` is required.
  - **Imap** — `CredLists` triggers the authenticated mailbox-list check.
  - **Sql** — `Kind` defaults to `"mysql"` but must be explicit; these authenticate against the
    database's own user table, not a system account. `fix_services_on_boxes()` binds
    mariadb/mysql to `0.0.0.0` and creates the credlist DB users (`CREATE USER … @'%'` +
    `GRANT ALL`), so the same credlist that satisfies SSH/SMTP logs in here and a healthy box
    scores UP. The box grants the DB users — don't drop `CredLists`. Keep box config + check in
    sync.
  - **Telnet** — Quotient has no protocol-aware Telnet check, but its generic `Box.Tcp` check
    (engine/checks/tcp.go: dials the port, scores UP on connect) is exactly enough to confirm the
    service is listening. Confirmed against `/opt/quotient`'s own source and
    `config/event.conf.example` on the scoring engine (2026-08-07).
  - **Windows entries** (`Enable WinRM`, `New SMB Share`, `RDP misconfigs`) — Quotient has no
    SMB/RDP/WinRM-aware check type at all (only Web/Dns/Ssh/Ftp/Smtp/Imap/Sql/Tcp exist), so
    these use the same generic Tcp port-open check. They're keyed by the exact nakon catalog
    config name: these are service-category Windows configs, not generic binary names, and
    `box_services.json` entries for a Windows box are catalog config names verbatim.
- **`build_event_conf()`** — box IPs use `192.168._.<last_octet>` where `_` is the team
  identifier placeholder (the wildcard every team's check matches). `StartPaused = true` keeps
  scoring held until `unpause_engine()`; `Delay 60` / `Jitter 10` / `Points 5` are the round
  cadence and per-check value. The `inject` account is emitted whenever an inject password is
  supplied (i.e. the competition has an `injects/` dir): Quotient's INJECTAUTH-guarded routes
  (POST /api/injects/create, announcements, submission downloads) accept `admin` and `inject`
  roles, so a dedicated inject manager lets an organizer run injects without the full admin
  login. Duplicate checks per box are deduped by (check key, port). `CredlistSettings` is emitted
  only when some check needs it — box-level `credlists` entries are just names, resolved by
  Quotient against the top-level registry (`config/credlists/<CredlistPath>` on the engine).
- **`_normalize_host()`** — Quotient's address comes off Terraform as a bare IP; requests needs a
  scheme.
- **`_admin_session()`** — Quotient's auth is cookie-based (POST /api/login sets a session
  cookie); there is no bearer token anywhere in the API.
- **`seed_teams()`** — looks team IDs up by name, then batch-updates identifiers + `active`, and
  sets the competition `started` flag (idempotent).
- **`unpause_engine()`** — unblocks the scoring round loop (`StartPaused=true`); **not
  idempotent**, gate it with a state flag.
- **`create_injects()`** — multipart POST per inject; skips titles already present. If existing
  injects can't be fetched, it proceeds without dedup and says so (a resume may create
  duplicates).

## terraform/main.tf

Bridges, scoring VM (1000), team1 VMs, NIC wiring, cold-boot + netplan.

- **`local.proxmox_host`** — bpg otherwise asks the API for each node's address for its SSH ops,
  which may not be reachable from the operator (e.g. it returns a LAN IP but the operator only
  has Tailscale); the endpoint host is used instead.
- **Provider `ssh` block** — used by bpg for operations the REST API can't do (e.g. file
  uploads). `insecure = true` for the self-signed cert most Proxmox installs have.
- **`proxmox_network_linux_bridge.team_bridge`** — **no `ports` attribute**: an empty bridge has
  no physical uplink, so teams can't escape onto the LAN.
- **`scoring_engine.vm_id = 1000`** — 100 collides with an existing unrelated VM on this node.
- **`agent { enabled = true }`** — needed to read back the real DHCP-leased management IP.
- **No cloud-init on the engine** — it has a dynamic IP and the template's baked-in netplan
  (dhcp4 on the mgmt NIC) suffices on its own; the template's own cloud-init build already baked
  in `var.ssh_public_key` and passwordless sudo for `var.vm_username`, so no bootstrap is needed
  before the driver connects.
- **Terraform provisions ONLY team1's boxes** — `create-competition.py` clones them to other
  teams via the Proxmox API after nakon has run. All bridges are still created here, because the
  scoring engine needs a NIC on every team bridge regardless.
- **`team1_key = "team1"`** — the rest of the pipeline (`collect_teams()` and hardcoded `"team1"`
  lookups) hard-assumes a team literally named `team1` exists; it's looked up **by name, not sort
  order**, so a non-default team-key set fails fast instead of silently building the wrong team.
- **vm_id `lifecycle` precondition** — the computed team_box vmid is checked at plan time against
  the scoring engine's fixed 1000, so a stride collision fails fast with a message instead of
  producing an API conflict at apply.
- **`clone { retries = 15 }`** — Proxmox locks the source VM; concurrent clones from one template
  race for the lock and the loser gets a short timeout. 15 retries ≈ 2 minutes of budget — enough
  for the winning clone to finish and release the lock.
- **`dynamic "disk"` only when `disk_gb` is set** — omitting keeps the template's own disk as-is;
  specifying null makes bpg default to 8 GB, which undercuts any real template disk and triggers
  an unsupported-shrink error.
- **`coalesce`, not `try`, for the disk interface** — a missing optional attribute is `null`, not
  an error, so `try(null, "scsi0")` returns null and fails validation at apply;
  `coalesce(null, "scsi0")` yields the default.
- **`dynamic "initialization"` only for non-Windows** — a Windows template (name contains "win",
  the same convention nakon's `os_to_platform()` uses) has no cloud-init/cloudbase-init agent to
  consume the block, so it would silently do nothing. Its IP/gateway/DNS and local admin
  credentials are set post-clone via guest-agent exec (`bootstrap_windows_box()`), which works
  over virtio-serial with no dependency on cloud-init or even a working network yet.
- **`user_account.password = var.box_password`** — nakon's paramiko connections use password
  auth; without it the cloud-init account has no password hash at all and every login attempt is
  rejected outright. Generated fresh per competition (see `variables.tf`), never a literal.
- **`dns.servers` is always a public resolver, never the team's `dns*` box** — pointing boxes at
  that box deadlocks provisioning: its bind9 is installed by nakon, and nakon installs it with
  apt-get, which needs a resolver that already works. `fix_dns_on_boxes()` forced 8.8.8.8 over
  the top anyway, so the `dns*` box was never actually serving its team — the two mechanisms just
  disagreed. To make it the real resolver, repoint the boxes after nakon has run (from
  `create-competition.py`), not here.
- **`scoring_mgmt_ips` exclusions** — loopback, `192.168.` (team subnets), Docker's `172.16/12`
  (Quotient runs in Docker on this VM), and Tailscale's CGNAT `100.64/10` (the engine may also be
  on a tailnet). No ordering guarantee — the first survivor is taken.
- **`null_resource.team_nics`** — triggers carry **identifiers only**: putting `var.teams` there
  would print team passwords in every plan. NICs are named predictably (ens18=mgmt, ens19=team1,
  ens20=team2, …); netplan **refuses world-readable configs**, hence `chmod 600`; sysctl sets
  `ip_forward=1` and `rp_filter=2`. Depends on the reboot resource: **the hypervisor-level cold
  boot must complete before `netplan apply` can find ens19/ens20**.
- **`null_resource.reboot_scoring_engine`** — a **cold boot is required: a guest reboot doesn't
  trigger the PCI scan** that detects newly attached VirtIO NICs; the VM is hard-stopped
  (`shutdown=0`) and started via the Proxmox API.
- **`null_resource.orchestrate`** — SSH-readiness probe; depends on the reboot (it must complete
  before the probe runs) and on `team_nics`, because this step reaches team1's boxes over the
  engine's team-facing NICs, so those must be addressed before nakon runs.
- **`local.team_vms`** — team1's box IP/gw/bridge are derived from `var.teams["team1"].identifier`
  + `box.last_octet`, matching `enumerate_targets()`.

## terraform/variables.tf

TF_VAR inputs; the long descriptions carry rationale worth preserving.

- **`vm_username`** — the built-in OS account already present on every template (not provisioned
  by us).
- **`box_username`** — the cloud-init account created on every team box; themeable per
  competition via `competitions/<id>/users.json` (`create-competition.py` writes
  `TF_VAR_box_username`); defaults to `ubuntu` when no `users.json` exists.
- **`box_password`** — generated fresh per competition in `deploy()`, not a fixed literal,
  because nakon authenticates with password auth rather than a key and a fixed value across every
  deployment would be guessable from this open-source repo. The username may vary per competition
  (`box_username`); only this password rotates.
- **`teams` defaults** — placeholders (the pipeline always overwrites `TF_VAR_teams`), but kept
  realistic (`101`/`102`) so anyone hand-running `terraform apply` builds addressing consistent
  with `192.168.0.0/16` and the team subnets at 101+ that the engine's NAT/isolation rules
  expect — not e.g. `192.168.1.x`.
- **`boxes_per_team.disk_gb`** — omitting it keeps the template's disk size (no resize), and also
  leaves the disk on the **template's storage pool**: `var.datastore` only applies when the disk
  block is emitted.
- **`boxes_per_team.disk_iface`** — defaults to `scsi0`; set `"sata0"` for Windows templates that
  boot from SATA (scsi0 needs virtio-scsi drivers the image may lack).
- **`boxes_per_team.template`** — must match a Proxmox VM tagged `template` exactly (see
  docs/usage-people.md, "Adding a template VM").

- **Per-box work runs concurrently (M2.1)** — `utils.run_concurrent(items, fn, max_workers=8)`
  is the one thread-pool helper. It returns an **index-aligned list** (each slot holds fn's
  return value or the exception fn raised — never raised here); an earlier dict-keyed version
  broke on unhashable items and was live-fixed 2026-09-24. Per-box budgets are independent
  (a slow early box no longer
  eats later boxes' patience), prints go through `utils.PRINT_LOCK`, and each function keeps
  its original aggregate semantics: `wait_for_boxes_ssh` and `fix_dns_on_boxes` abort only
  when **all** boxes fail; `prep_apt_on_boxes` / `fix_services_on_boxes` warn per box;
  `setup_ubuntu_auth` raises when any box ends without working auth (all boxes still get
  attempted — better diagnostics than the old first-failure abort). The pool cap (8) sits
  well under the engine's raised sshd `MaxSessions` (64). `fix_services_on_boxes` keeps the
  per-box script *construction* serial (pure string building) and parallelizes only the
  SSH/guest-agent execution.

## Conventions

Repo-wide rules the code (and AGENTS.md) enforce; repeated here because every module above
depends on them.

- **nakon is a CLI dependency only.** `generate_nakon_config()` calls `nakon randomize --json`
  (via `_nakon_randomize`), `build_nakon_bundle()` runs `nakon build`, and `run_nakon()` runs
  `nakon deploy` on the engine over SSH — always as a subprocess, never an in-process `from nakon
  …` import, and never a direct MySQL connection. All catalog access goes through nakon.
- **`vendor/nakon` is pinned to a release tag** and bumped deliberately: check out the tag in the
  submodule, then commit the new pointer. Don't develop nakon inside this checkout — work in the
  nakon repo, tag a release, then pin it here.
- **`vendor/nakon/.env`** (gitignored) holds the vulndb creds for build/randomize time; the
  bundle itself carries no credentials to the engine.
- **`NAKON_DIR = Path("vendor/nakon")`** — nakon subprocesses run with that as cwd (so they can
  read the `.env`); bundles live under `vendor/nakon/bundles/`, content-addressed and shared
  across competitions.
- **Per-run secrets are gitignored** (`teams.json`, `event.conf`, `credentials.txt`,
  `nakon-config.json`, `.deploy_state.json`); non-secret artifacts are tracked (`boxes.json`,
  `Compfile`, `box_services.json`).
- **`--from-phase N` resumability** — pinned `box_services.json`/`box_vulns.json` make re-runs
  deterministic: the same selection produces the same bundle-cache hit, so a resumed deploy
  replays instead of re-randomizing.
