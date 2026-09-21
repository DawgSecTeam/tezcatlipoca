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
- **`build_nakon_bundle()`** — `nakon build --config <abs> --out bundles --json`;
  content-addressed under `vendor/nakon/bundles/`, cached when the catalog is unchanged; runs with
  `cwd=NAKON_DIR` for vulndb creds (same error-message contract as `_nakon_randomize`).
- **`run_nakon()`** — scp's the nakon package, bundle, and config to `/tmp/nakon` on the engine,
  then runs `nakon deploy` from `/opt/nakon`. `only=` scopes to machine names **without changing
  bundle content**; `strict=True` passes `--strict` so failures abort instead of just reporting
  live. On `TimeoutExpired`, the handler `pkill -9 -f 'nakon deploy'` on the engine: the client
  timeout doesn't reliably kill the remote process, and a leftover deploy would race a second
  one.
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
  step runs in its own isolated nakon pass (`_run_single_nakon_config`). `promote_dc=False` only
  rejoins members (the redeploy case).
  - **ADDS resume artifact** (`.nakon-domain-<team>-adds.json`): its existence means ADDS already
    ran for this team — re-running `Install-ADDSForest` on a live DC would just fail. Promotion
    is skipped on resume; the joins below still run (they're the recoverable part).
  - **AD DS promotion budget: up to 20 min** (`wait_for_guest_agent` timeout 1200) — promotion is
    slow and ends in a reboot.
  - **AD misconfigs run with `strict=False`**: this pass is scoring flavor, not range
    infrastructure, and can fail non-fatally even with nakon ≥ v0.1.3 (which fixed the duplicate
    vars-less dependency step): "Disable System Firewall" sweeps every AD computer over WinRM, and
    a Linux realmd member has no WinRM, so that step exits 1 on any mixed Windows/Linux domain
    after landing its own misconfig. Failures print in nakon's summary instead of aborting.
  - DC DNS SRV records are awaited (`wait_for_dc_dns`) before any member join.
  - **Windows members** get DNS repointed at the DC first, then `Domain Join` (reboots);
    **Linux members** join via nakon's `domain-join` (realmd/sssd) — no reboot and no separate
    DNS repoint needed.
  - **Linux member join failures are scenario flavor, not infrastructure**: the box keeps local
    auth and every scored service. The apt install of the realmd stack can blow past nakon's
    per-step timeout on small VMs — log loudly and keep deploying (hence `strict=False` + catch).

## deploy.py

The seven-phase orchestrator and CLI. `deploy()` owns phase sequencing, resume, and credential
generation.

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
- **Bundle built operator-side** — the nakon bundle is built here, on the operator machine, from
  the FULL machine list: the only step that needs the vulndb (MySQL + vulndb-ui/MinIO), which
  `generate_nakon_config()` has already proven reachable. Phases 5 and 6 both deploy from this one
  bundle, so **the scoring engine never sees vulndb credentials**.
- **`teams.json` persisted** — `destroy-competition.py` requires it to tear the competition down
  later; verification also reads team logins from it instead of re-deriving.
- **`all_targets` built once from the FULL box list** — see `enumerate_targets()`'s invariant:
  nothing downstream may re-derive a vmid from a filtered `boxes` (vmid is positional).
- **`confirm_deploy()` skipped on resume** — resuming implies prior confirmation; `--yes` skips it
  outright on a fresh deploy.
- **Sweep marker reset** — a fresh deploy unlinks `.phase6-swept` so it never inherits a previous
  deploy's marker.
- **[2/7]** — `terraform apply -parallelism=1`: concurrent full clones saturate the
  datastore/API (HTTP 596). Apply timeout is `2400 + 1800` per Windows box (60 GB clones vs 15 GB
  Linux). The engine's SSH reachability is **polled** (`wait_for_ssh`) instead of a blind
  post-apply sleep.
- **[3/7] removed** — the old phase copied an SSH key to the engine, but nothing ever read the
  copy back: all SSH/SCP uses the local key via ctx, and nakon authenticates to team boxes by
  password. The phase number is retained (as a no-op) for `--from-phase` stability. The shared
  ctx/key/ip setup runs even when phase 3 itself is skipped, because phases 4–7 reference it.
- **[4/7] ordering** — `event.conf` is pushed BEFORE nakon: that prevents the Quotient crash loop
  that wipes NAT; `ensure_nat_forwarding()` immediately after.
- **[4.5/7]** — team1 Windows boxes are bootstrapped here (no cloud-init on Windows to set
  IP/DNS/credentials), and `setup_ubuntu_auth` runs for team1 Linux. Only team1 exists at this
  point; team2+ are cloned later.
- **[5/7]** — DNS fix on team1 (nakon needs working apt), `tz-base` snapshots (pre-nakon restore
  point), team1's machine names filtered from the full nakon config by third octet, then
  `run_nakon(only=…)` — `--only` scopes the deploy; the bundle is still the full one. Timeout is
  `max(2400, PER_MACHINE_NAKON_BUDGET × len(team1_machines))`.
- **[6/7]** — the clone + team2+-nakon sweep is gated by `.phase6-swept`, written only after a
  clean sweep, so a mid-phase-6 resume skips straight to domains/beacons/hardening instead of
  re-running the sweep. Single-team competitions harden team1 here instead of running nakon
  again. Team beacons (if `team_beacons 1`) are planted **before** the `tz-ready` snapshot so
  clones and restore points carry them.
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
  or already-gone VMs are tolerated silently.
- **`load_destroyable_competitions()`** — requires `Compfile` + `teams.json` + `boxes.json`:
  competitions deployed before `teams.json` support can't be safely destroyed this way.
- `terraform destroy -parallelism=1`, matching the apply.

## redeploy-competition.py

Filtered per-team/box rollback/reconfigure/rebuild against a live range, using snapshots.

- **Modes** — `rollback-ready` (tz-ready, the default; seconds), `rollback-base` (tz-base, then
  re-nakon + hardening + domain), `reconfigure` (no rollback — run the configure chain against
  the live boxes), `rebuild` (recreate from template). Any rollback **discards everything the
  defending team(s) did to those boxes** — the confirmation prompt says so.
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
