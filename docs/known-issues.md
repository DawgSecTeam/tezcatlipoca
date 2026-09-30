# Known issues & incident log

Live-confirmed incidents, standing failure modes, and limitations of this range — the things
that have actually burned a deploy or a scrim. Design consequences live in
[internals.md](internals.md) (deploy pipeline) and [scrim-harness.md](scrim-harness.md) (agent
harness); symptom-level fixes live in [usage-people.md](usage-people.md)'s Troubleshooting table.

## Live-confirmed incidents

### svc-matrix-2026-09-28: four plant/bring-up defects on the all-services matrix (all FIXED)

The full 16-pin service matrix (every `_SERVICE_TO_CHECK` entry, see the run report
[svc-matrix-2026-09-28-report.md](svc-matrix-2026-09-28-report.md)) surfaced four independent
defects, each of which took a scored service DOWN on both teams until fixed:

1. **nakon bind plant duplicates `zone "localhost"`** — the catalog's bind config declares
   `zone "localhost"` in `named.conf.local`; Ubuntu's `named.conf.default-zones` already
   declares it, and named exits at config parse (rc=1, no journal). FIXED in
   `fix_services_on_boxes` (strips the duplicate; the default-zones copy serves db.local,
   whose `A localhost → 127.0.0.1` is exactly what the Dns check resolves).
2. **lighttpd port move never landed** — the splunk shim edits `server.port = 8000` then
   `systemctl start`s lighttpd, but the plant had already started it on :80 and `start` is a
   no-op on an active unit. FIXED: `restart`.
3. **RDP: registry flip ≠ reachable** — the `RDP misconfigs` plant sets `fDenyTSConnections=0`
   but the template ships the RemoteDesktop firewall rule group disabled, so :3389 listens and
   still drops every external dial. FIXED: `bootstrap_windows_box` enables
   `RemoteDesktop-UserMode-In-TCP/UDP`.
4. **Golden clones inherit the template disk with no resize** — terraform sizes team clones
   from `disk_gb` but goldens are plain clones, so a splunk golden ran on the 15 GB ubuntu
   template disk and the .deb unpack hit ENOSPC mid-plant (golden aborted, deploy died at
   phase 4). FIXED: `golden_ops.ensure_golden_disk_size` grows the clone to `disk_gb`
   (grow-only) before first boot, so cloud-init's growpart expands the guest fs natively.
   Manual-recovery note if you ever meet the old state on a deployed range: resize the golden
   AFTER rolling back to tz-base (a rollback reverts a pre-snapshot resize), then delete
   tz-base so a phase-4 resume keeps the grown disk.

### Golden disk resize never reaches an LVM root fs — big plants die with ENOSPC (regression-4x1-2026-09-28)
*(fixed 2026-09-28: `golden_ops.expand_guest_root_disks` — growpart + pvresize + lvextend + resize2fs post-boot, before the pre-plant snapshot)*

`ensure_golden_disk_size` grows the hypervisor disk correctly, but the guest-side
expansion premise ("cloud-init growpart expands natively") only holds for PLAIN
partition layouts. Ubuntu cloud images run root on LVM: sda3 keeps its original size,
pvresize/lvextend never run, and the root LV stays at its template size (10G on
base-ubuntu24.04-fix) no matter how big the disk grows. svc-matrix masked this —
web03 ran splunk alone (~3G) under the ceiling — but four services on one box
(apache + roundcube + splunk + bind) filled the 10G LV and bind's apt died with
`E: Write error - write (28: No space left on device)`. The expand step runs after
first boot and before the pre-plant `tz-base` snapshot, so the rollback point carries
the expanded fs and re-entry replants cleanly.

### DC template ships all firewall profiles disabled (svc-matrix-2026-09-28)
*(fixed 2026-09-28: `bootstrap_windows_box` runs `Set-NetFirewallProfile -All -Enabled True` before its rule enables)*

`base-windows-server`-derived DCs come up with Domain/Private/Public firewall profiles all
`False`. Consequences: firewall-rule effects (and any blue hardening that assumes rule state
matters) do nothing on a DC until `Set-NetFirewallProfile -All -Enabled True`; and the ADDS
takedown must NEVER stop NTDS — on a DC the Administrator login authenticates against the AD
database NTDS serves, so stopping it locks out every SSH foothold (bad-auto locked itself out
and needed a host-side VM reset; its ADDS effect is a port-block rule + profile enable now).

### cyberrange (.150) operational notes (svc-matrix-2026-09-28)

- An **orphan `quotient-engine` runs at vmid 1000** on node proxmox — the default
  `--scoring-vmid`. Always pass an explicit free `--scoring-vmid` on this node.
- Datastore reality vs the preflight gate (need ≈ teams × Σdisk_gb): hdd 380 GB, ssd 365 GB,
  wkshp-pool ~748 GB free. A 10-box × 2-team comp only fits **wkshp-pool** (user-approved for
  svc-matrix; thin-provisioned actual usage is far below the provisioned gate number).
- The realm env variant `.env.realm-backup-20260923` (targets .150) carried a stale
  `TF_VAR_template_vm_id=9106` (the engine base preflight hard-fails on it) — fixed in place
  to 955 (`base-ubuntu24.04-fix`).

### box_username colliding with a legacy distro account bricks auth setup (distro-matrix-2026-09-27)
*(fixed 2026-09-28: generate-time + users.json lint `is_legacy_account_name` falls back to the default with a warning)*

`box_username operator` (a users.json choice) collides with Fedora's legacy `operator`
system account (uid 11, shell `/usr/sbin/nologin`, home `/root`) — Debian-family images carry
the same uid-11 `operator`. cloud-init finds the name already in `/etc/passwd` and adopts it:
the sudoers rule and the SSH key land on a nologin root-homed shadow of the real account, no
fresh user is created, and every `ssh operator@box` attempt fails no matter what. Phase 4's
8-attempt auth ladder then burns out and aborts the deploy. The same seed on Alpine (no legacy
`operator`) created a real user and passed instantly — the failure is purely per-distro
`/etc/passwd` heritage, which is why it never surfaced on the ubuntu/debian lineup.

The guest-agent fallback in `setup_ubuntu_auth` cannot rescue this on SELinux-enforcing guests
(fedora): the qemu-ga SELinux context cannot write `/etc/ssh/` or `/etc/sudoers.d/`
(`sed: couldn't open temporary file ... Permission denied`), so the fallback dies at rc=1 even
as root.

Mitigation: pick a `box_username` that is not a legacy account name on ANY target distro
(`medic` is the proven safe choice; `operator`, `daemon`, `games`, `mail`, `news`, `sync` and
friends are landmines). Documented here rather than linted — the collision set is distro-specific.

### Alpine cloud-init never writes /etc/resolv.conf — package installs silently no-op (distro-matrix-2026-09-27)

Alpine's cloud-init (v25.3, ENI renderer) writes `dns-nameservers` into
`/etc/network/interfaces` but nothing on Alpine creates `/etc/resolv.conf` (no resolvconf
hook — Debian's ifupdown handles that; Alpine's ifupdown-ng does not). Every DNS lookup in the
guest then times out: `apk update`/`apk add` each burn ~10 s and fail with
`unable to select packages: bash (no such package)` (empty index), so cloud-init's
`packages:` directive installs nothing. Worse, the package module is once-per-instance, so
rebooting and retrying does NOT retry the install — the instance semaphore is already written.

This hits template BUILD bootstraps (the builder's `packages:` list silently missing) and any
clone expected to use DNS before the driver's `fix_dns_on_boxes` runs (that pass writes
`/etc/resolv.conf` directly, so in-range nakon plants are unaffected). Fix for template
building: add `manage_resolv_conf: true` + `resolv_conf: {nameservers: [...]}` to the
bootstrap user-data (it runs before the package module), or bake `/etc/resolv.conf` into the
template via chroot. See `competitions/distro-matrix-2026-09-27/build_alpine_ci_template.sh`
for the working Alpine 3.23 cloud-init template recipe.

### sshd start-limit crash loop (2026-09-03)

Several catalog configs (`ssh-root-login`, `ssh-empty-passwords`, `ssh-password-auth`,
`ssh-max-auth-retries-high`, `ssh-x11-forwarding`, …) each independently run
`systemctl restart ssh`. Landing several on one box back-to-back with no delay trips systemd's
crash-loop protection (`Result: start-limit-hit`), leaving sshd down for the rest of the
competition. Confirmed live on web01-team101: 5 `ssh-*` configs, journalctl showed 5 restarts
inside the same second.

Mitigation (hardening_ops): misconfig passes are spaced so restarts don't land in the same
instant, and the SSH script ends with `systemctl reset-failed ssh` + a conditional start, falling
through to the guest-agent fallback when SSH is already gone — either way sshd ends the pass
running.

### Clones come up without a routable address (e2e-2026-09-19)

Clones can boot without the `ipconfig0` address (a cloud-init race), and ifupdown loses the
address on any later carrier blip. First seen on app01; before the fix this surfaced as a phase
6/7 failure an hour after cloning. Mitigation (clone_ops): `_repair_box_network` re-writes the
address, `ensure_cloned_network` gates phase 6/7 on every box actually holding its IPv4, and
guest-agent exceptions count as "not yet" so a merely-slow cloud-init is never "repaired".

*(extended 2026-09-24, e2e #3)*: two deeper causes surfaced behind the same symptom. **(1) Full
clones get a NEW NIC MAC, but cloud-init's `50-cloud-init.yaml` still matches the SOURCE's MAC**
— `ipconfig0` notwithstanding, no address ever applies; the box boots addressless on every boot.
**(2) `_repair_box_network`'s "first non-loopback interface" selection can pick `docker0`**
(web01/db01 run docker), persisting the static config onto the wrong interface, and the real NIC
flaps between `eth0`/`ens18` (netplan `set-name` vs predictable naming), so even a correct
runtime `ip addr add` gets flushed by the helper's own `systemctl restart systemd-networkd`.
Repair that survives (guest-agent exec, no SSH needed): write `/etc/netplan/99-tz-static.yaml`
matching the MAC read from `/sys/class/net/<if>/address` (no `set-name`, single default route),
`rm -f /etc/systemd/network/90-tz-static.network`, `netplan apply`. The old netplan must be
replaced entirely if it declares a second default route — `netplan apply` errors out on the
conflict and applies nothing.

### Planted Linux boxes deny all SSH at PAM account stage after a restart (e2e #3, 2026-09-24)
*(root cause UNSOLVED — workaround: rebuild the box, or never restart a planted one.
2026-09-29 research pass: the visible pin set does NOT reproduce it; the prior session's
bisect results recovered; the pam-lab built for it (vmid 1181) was destroyed in the same
day's cleanup — rebuild from a base-ubuntu24.04-fix clone + the plant rungs below)*

Phase-5-planted team1 Linux boxes (web01, db01, and app01 in its first life) stop accepting SSH
entirely after their next stop/start: the TCP handshake completes, the connection dies at
preauth — paramiko reports "Authentication failed: transport shut down or saw EOF", sshd logs
`fatal: Access denied for user <u> by PAM account configuration [preauth]`, and the audit record
says `op=PAM:accounting grantors=?`. Everything that could explain it verifies clean: sshd binary
and config (`dpkg -V`, `sshd -t` rc=0), PAM modules, stock `common-account` (pam_unix →
pam_permit cannot fail), no nologin files, valid shadow, and `unix_chkpwd <u> chkexpiry` returns
rc=0 for every user. strace shows pam_unix read the whole shadow, then fail with no syscall in
between. The box's sshd works immediately after its plant and breaks on the next boot; a full
disk rebuild (`redeploy --mode rebuild`) fixes it, so the damage is disk-persistent but
boot-triggered. The planted backdoors (`auth sufficient pam_permit.so` prepended to
`common-auth`, su's `[success=done] pam_permit`) are scoring vulns, NOT the cause. Diagnostic kit
that got this far — all guest-agent-driven (SSH is the thing that's broken): agent-exec
`sshd -t`/`journalctl -u ssh`; a debug daemon on an alt port via `cp /usr/sbin/sshd /tmp/sshd2`
(the PAM service name is argv[0] — `-o PAMServiceName` does not exist in OpenSSH 9.6) launched
with `setsid` (guest-agent exec kills its children when the exec session ends); `strace -f`
around the failing connect; a `pam_exec`-probed copy of the account stack on the `sshd2` service
to bisect.

**2026-09-29 research pass** (fresh noble 24.04 lab, vmid 1181 "tzlive-pamlab" on .150):

- **The visible catalog cannot produce the failure.** The complete e2e-09-19 web01 pin set
  (36 scripts planted verbatim: pam-permit-empty, pam-no-password-quality, pam-permit-auth's
  common-auth prepend, 4× uid-0 users, 666 shadow/passwd/crontab, cron persistence, journald
  100%, the +12h `date -s` clock shift, the five ssh-* pins with their restart storm, …)
  followed by a hard `qm stop/start` cycles cleanly — pubkey AND paramiko-password dials work
  for the operator and freshly planted users. Note the two pam pins are also *incapable* of
  touching sshd's chain on Ubuntu: `pam-permit-empty` only edits `su`/`login` (no
  `system-auth` here) and `pam-no-password-quality` matches no Ubuntu file; the only
  common-auth toucher (`pam-permit-auth`) is auth-stage and cannot yield an *account*-stage
  sshd refusal.
- **The prior session's bisect conclusions (recovered from its scripts in
  `scrim-runs/patch-*.py`, Sep 25): the failure was SYSTEM-WIDE account-stage misbehavior** —
  `pam_nologin` failed with no nologin file present, `pam_unix` account failed while
  `unix_chkpwd` passed, and **sudo's account stage was broken too**; the verified-working
  repair was replacing `common-account` with a single `account required pam_permit.so`
  (`patch-common-account.py`, which also left `common-account.tz-backup` — no such backup
  survives on any live box; the incident VMs are gone and vmids 1332/1333 were recycled to
  the pfsense-rvb range). Every individual account module failing against verifiably-clean
  inputs rules out pin content and module logic: the fault is stack-level (config parsing,
  NSS resolution, or module loading) inside the account stage's environment.
- **Boot-triggered + per-invocation reads is the key tension.** PAM configs are read
  per-invocation (the patch heals in-flight plants immediately), so a purely corrupt config
  file would fail at plant time, not after a reboot. The boot trigger therefore lives in what
  the account stage *touches* at boot — /run state (fresh tmpfs each boot), NSS module
  resolution, or something rewriting `/etc/pam.d/*` at boot that was never identified. The
  heal-by-rebuild is consistent with a boot-ordering race rather than deterministic state.
- **Standing instrument (destroyed 2026-09-29 in the range cleanup — rebuild on demand):**
  the pam-lab was ubuntu24.04-fix + the FULL plant above, with the corrected bisect harness
  — `/etc/pam.d/sshd2` probes the account stage stage-by-stage via `pam_exec`, sshd2 debug
  daemon on port 2225; healthy
  signature is `probe-step0/1/3` in `/tmp/pam-bisect.log` with **step2 ABSENT** (pam_unix
  success skips it via `[success=1]`); a broken box shows `probe-step2-*` (and the failing
  step before it missing). Reproduce with the next real phase-5 plant + restart, then dial
  2225 and read the log. (pam_exec probes must be argument-direct — `/bin/echo probe-x` —
  the old `/bin/sh -c 'echo x'` form loses its quotes through every transport.)

### Phase-6 mid-crash resume is safe; Windows-clone bootstrap needs the agent up (~8 min) (e2e #3, 2026-09-24)
*(fixed 2026-09-27 in d1e5763: `bootstrap_windows_box` polls setup-complete + retries the exec within a 900 s deadline instead of one 90 s shot)*


`clone_team_boxes` skips existing clone vmids on resume ("already exists — skipping clone
(resume)") and re-applies `net0`/`ipconfig0` idempotently, so a crash mid-phase-6 resumes clean.
The one sharp edge: `bootstrap_windows_box`'s 90 s guest-agent timeout against a Windows clone
whose agent takes ~8 minutes to appear — the first post-crash resume died exactly there (vmid
1340). Re-running the resume once the clone has been up a few minutes works; bootstraps re-run
idempotently.

### Quotient cold start exceeds 60 s (e2e-2026-09-19)
*(fixed `63b9c31`/`0c0547e`, 2026-09-19)*


The first `compose up` after a `--no-cache` build cold-starts Postgres (initdb) and recreates
every container — measured >60 s twice in that run. The engine bootstrap timeout is therefore
600 s (engine_ops), not a "generous" round number.

### opencode dies 40/40 with "Unexpected server error" (agent-scrim-2026-09-17c)
*(fixed 2026-09-18 in the 17c launch hardening)*


Every blue session launch in the 17c run failed instantly with an opencode server error caused
by the launch context (a `capture_output` thread launch) — a failure that never reproduced when
run in the foreground. `run-agent-scrim._opencode_run` is hardened instead of trusting the
context: no stdin, own process group, per-team `HOME`/`XDG_*` so opencode's state DB and server
logs live inside the run dir.

### Red evidence nearly lost to scp flakes (2026-09-17b)

red01's `events.jsonl` is the only complete record of what red did. The direct
operator→red01 scp hung past 60 s in the 09-17b run; the file had already been at risk twice.
`run-agent-scrim` now grabs it at every snapshot tick (a teardown-time flake can't lose it) and
falls back to tunneling through the scoring engine as a jump host.

### "Scoreboard unreachable" / `{"error":"Forbidden"}` — three-round mystery
*(fixed `f71a80a` + `a98ee49`, 2026-09-19/20)*


Two independent causes, each of which produced weeks of confusing Forbidden errors:

- **Quotient allows ONE session per account.** Every login kills that account's previous cookie,
  so any ad-hoc login (browser, curl debugging) silently invalidates the harness's session.
  Rule: never log in ad hoc; run `./qlogin` only when a request returns `Forbidden`. The newest
  login always wins, so concurrent qlogin refreshes converge on the only valid cookie.
- **The services API keys on the engine's own IDs** (1, 2, …). Querying it with the subnet
  identifier from `teams.json` (101, 102) answers `Forbidden` even with a valid cookie — the
  second half of the mystery (`_team_tid` in run-agent-scrim maps team → engine ID).

### Datastore saturation under parallel clones (HTTP 596)

Concurrent full clones saturate the datastore/Proxmox API (pvestatd hangs, HTTP 596 errors).
Terraform therefore runs with `-parallelism=1`, the apply timeout scales per Windows box
(60 GB vs 15 GB Linux images), and Proxmox task waits budget 1800 s for slow storage.

### Proxmox source-VM lock race

Proxmox locks the source template during a clone; concurrent clones from the same template race
for the lock and the loser gets a short timeout. `terraform/main.tf` gives clones `retries = 15`
(~2 minutes of retry budget) — enough for the winning clone to finish and release.

### Docker wipes iptables on every start/restart

Docker re-syncs iptables on any container start/restart: `FORWARD` policy goes to `DROP` and the
custom team-subnet MASQUERADE + team-to-team DROP rules vanish. The MASQUERADE loss is silent —
nakon swallows the resulting apt failures (`install_package` ignores exit status), so services
just don't install. Mitigations (engine_ops): `ensure_nat_forwarding` re-asserts both rules
before each nakon pass, `range-firewall.timer` re-asserts them every 30 s for the life of the
range, and `range-healthcheck.timer` logs rule/container status every 60 s.

### llama.cpp context ceiling (agent-scrim-2026-09-16)

opencode's own base prompt is ~20k tokens; the endpoint's advertised context must leave room for
it or opencode self-compacts fatally. 60000 was verified against the slot limit — don't raise
model context claims above what the slot actually holds, and don't set `reasoning_effort`.

### Engine DB password with `#` crash-loops the scoring engine (scrim-extreme-cyberfield-2026-09-22)
*(fixed `caf3dd0`, 2026-09-22)*


`quotient_server` restart-looped 620 times: the generated postgres password contained `#`, and
`postgres://engineuser:#…@host/db` is a URL with a fragment — the DSN truncates at the password,
the app connects to a garbage host, and dies in ~80 ms with zero DB-side auth attempts (DB
healthy, psql fine, fresh container fine — only a malformed DSN explains all of it).
`random_password()` now draws symbols from `!*_-+=` only: URL-grammar characters (`#/?@`) never
enter anything that lands in `/opt/quotient/.env`.

### The plant is NOT idempotent over a planted box (scrim-extreme-cyberfield-2026-09-22)
*(fixed `caf3dd0`, 2026-09-22)*


Re-running phase 5 over already-planted boxes produced 11 new failures in two clean classes
(Linux "transport shut down or saw EOF" once a planted pin breaks SSH, Windows idempotency
errors on `Elevate Guest Account`/`never-expires-service-account-win`/`iis-webshell`) and burned
a repair cycle. `deploy()` now auto-rolls team1 boxes back to `tz-base` on `--from-phase 5`
re-entry before re-planting; `take_snapshot` replaces an existing snapshot, so the restore
point is genuinely re-taken at the start of each phase-5 pass.

### `sshd-force-sftp-broken-chroot` kills SSH on every Linux box (scrim-extreme-cyberfield-2026-09-22)
*(pruned from pin sets `f05c661`, 2026-09-22; upstream vulndb script fix still pending)*


The catalog script appends `Match Group sftpusers` to `sshd_config` but never creates the group;
sshd treats a `Match` on a nonexistent group as a fatal config error, the script's
`systemctl reload … || true` hides the failure, and SSH dies at the next sshd restart. This one
pin blocked red's foothold expansion, blue's hunts on all 6 Linux boxes, the verifier's
misconfig check, and phase-6's own DNS step (all fell back or failed). Dropped from both
extreme pin sets until the script is fixed in the vulndb. Companion fixes: `verify-competition`'s
misconfig check falls back to guest-agent exec when SSH is dead.

### Noble dpkg-breakers: `tftpd-hpa-anon-write`, `postgresql-remote-access` (scrim-extreme-cyberfield-2026-09-22)
*(pruned from Noble pin sets `f05c661`, 2026-09-22)*


`tftpd-hpa-anon-write`'s dpkg postinst exits 82 on Ubuntu Noble, wedging dpkg so every later
apt/dpkg step on the box fails in cascade (looks like 8 broken configs, 1 real cause);
`postgresql-remote-access` pulls the already-known-bad `postgresql-no-auth`. Both stay pruned
from Noble boxes (Debian 13 boxes tolerate them) until their scripts get pre-seed fixes. The
5 Windows user-policy pins (`local-user-win`, `powershell-execution-unrestricted`,
`rpc-proxy-on-dc-web-win`, `unauth-kiosk-app-startup-win`, `mailenable-cleartext-mail-win`) stay
pruned too — `Set-LocalUser`/password-policy failures on users their own config was supposed to
create.

### Blue cycles rc=1 without a shell parent; timeouts hung on the server grandchild (2026-09-22)
*(fixed `2e5a976` (shell parent) + `beff624` (killpg timeouts), 2026-09-22/23)*


Two more opencode launch lessons on top of the 17c hardening: opencode's server dies silently
right after "llm runtime selected" when the CLI is exec'd directly from python (manual runs,
pty, env — all fine; only a shell parent as the process's parent differs), so `_opencode_run`
spawns `bash -c 'exec opencode run …'` with the prompt passed via `$CYCLE_PROMPT` (no
shell-quoting of a multi-KB prompt). And `subprocess.run(timeout=…)` hung ~80 min past the
timeout because opencode's server grandchild held the pipes — timeouts now `os.killpg()` the
whole session. `CYCLE_TIMEOUT` is 1800 s.

### Teardown dies half-done on out-of-band deleted VMs (scrim-dress-2026-09-20 run-12)
*(fixed 2026-09-23: destroy retries once before giving up)*


`terraform destroy` failed partway when manually-deleted `-fix` templates were missing from the
node; the range was left half-torn-down. `destroy-competition.py` now retries the destroy once
(every pass makes progress on the remaining resources) before giving up with a pointer to
`terraform state list`.

### Every inject "closed" after a rollback-ready rerun (winad-scrim2 2026-09-26)
*(fixed 2026-09-26: `--reset-event`, verify WARN)*

Inject offsets resolve to absolute times at phase 7. `redeploy-competition.py --mode
rollback-ready` resets the boxes but not the engine, so a rerun hours later found all 12 injects
closed ~1.5 h before T0. For a scrim rerun use `--mode rollback-ready --reset-event`: it
re-clones the engine from its template (fresh scoring DB) and re-runs phase 7, re-anchoring
injects at now. `verify-competition.py` WARNs on any inject already past close.

### Killed driver left terraform running (winad-testrun 2026-09-25)
*(fixed 2026-09-26: `utils.run_terraform`)*

Killing the Python driver orphaned `terraform apply`, which kept the state lock and kept
mutating infra. Terraform now runs in its own process group; SIGINT/SIGTERM/timeout forward
SIGINT (graceful stop, lock released), then SIGKILL after 60 s. A SIGKILL of the driver itself
still can't be caught — `pkill -INT terraform` by hand in that case.

### Interrupted clone blocks every later deploy (winad-testrun 2026-09-25)
*(fixed 2026-09-26: clone marker + unlock + orphan-disk GC)*

A host reboot / kill mid-clone left an untagged, `lock: clone` VM in a golden/team slot that
preflight called "foreign". Every clone POST now carries `description: tezcatlipoca-clone
comp-<name>` (the clone API takes no tags), which preflight accepts as ours. Phase 1 clears the
stale lock and destroys with `destroy-unreferenced-disks`; orphaned `vm-<vmid>-disk-N` volumes in
an empty slot are deleted before cloning. PVE only lets `root@pam` clear `lock` — with a token
the error names the exact `qm unlock <vmid> && qm destroy …` to run on the node.

### Account-wide rate limit killed both blue agents at once (winad-scrim2 2026-09-26)
*(mitigated: `run-agent-scrim.py --blue-watchdog`)*

Bounded shifts don't help when one 429 session limit kills every agent until its reset; web01
sat down ~2 h unwatched. `--blue-watchdog` runs a non-LLM loop (every 60 s) that
unmasks/enables stopped scored Linux units over the operator key. It keeps services up; it does
not hunt. Also leave slack against the account's usage window before committing to a fixed
scrim end time — there is no automated guard for that.

### Bundle var-lint false-flagged a valid deploy (scrim-live 2026-09-27)
*(fixed 2026-09-27: `nakon_ops.py` strips/guards + `tests/test_bundle_lint.py`)*

A fresh deploy from the agent-scrim template aborted at bundle build: the var-lint reported
theme-wallpaper `DEST`, local-user `GROUPS_ADD`, systemd-service `PAYLOAD_BODY/PATH/EXTRA_LINE`,
apache-site `EXTRA_DIRECTIVES` as undeclared. All were false positives — the catalog scripts
self-guard (`${VAR-}`, `[ -n "${VAR-}" ]`) or self-assign (`DEST=`). Four lint gaps: it only
stripped the `${VAR:-}` expansion (not `-`/`:+`/`+`); its `[ -n ]` guard regex missed the braced
`${VAR-}` form; its single-quote strip crossed newlines (an apostrophe in a comment ate the
`DEST=` line); and it never stripped full-line comments (a var named only in prose). Fixed; a
genuinely-required `${VAR:?}` is still caught (regression test).

### Engine bootstrap loses the apt archives lock (pfsense-ad 2026-09-27)
*(fixed 2026-09-27: `engine_ops.py`)*

Phase 2 engine-template build failed `rc=100`, `E: Could not get lock
/var/cache/apt/archives/lock … held by process NNNN (apt-get)`. First-boot
`apt-daily-upgrade.service` re-spawns an apt-get *between* the preamble's killall and the
bootstrap's `apt-get upgrade`, and `DPkg::Lock::Timeout` only covers the dpkg frontend lock, not
the archives-cache lock. `systemctl stop` lost the race twice in a row. Fix: `systemctl mask
--now` the apt-daily/unattended-upgrades units (a masked unit can't be re-activated), kill their
cgroups, then poll `fuser` on all three locks until free before touching apt.

### apt-prep + settle-check assumed Debian (distro-matrix / pfsense-ad 2026-09-27)
*(fixed 2026-09-27: `hardening_ops.py`, `tests/test_non_apt_prep.py`)*

On a fedora (dnf) or alpine (apk) box the per-box apt-prep printed `apt-get: command not found`
five times and the settle-check burned the full 240 s budget every pass. Both now short-circuit
with `command -v apt-get >/dev/null 2>&1 || exit 0` before any apt call — a non-apt box is an
honest rc=0 no-op.

### Fedora member needs a "-fix" template + per-box service fixes (pfsense-ad 2026-09-27)
The stock `base-fedora44` has no cloud-init (can't take identity), and the Fedora Cloud image
differs from Ubuntu in ways that broke the plant and the scored services: SSH password-auth is off
(`/etc/ssh/sshd_config.d/50-cloud-init.conf`, and sshd is first-match-wins so the override must be
`00-*`); `named` binds `127.0.0.1` only; `httpd` hangs at boot resolving its `ServerName` against
the not-yet-up DC; the qemu-guest-agent flaps after reboot. Resolved by building `base-fedora44-fix`
(cloud-init + `00-tzc-pwauth.conf`) and, per box, `ServerName localhost` + `named listen-on { any; }`.
The per-box httpd/named fixes are folded into `fix_services_on_boxes` (docroot + `ServerName
localhost` + named listen-on/allow-query + resolved stub-off), so they re-apply on every fresh
clone — the old "fold into the golden/catalog" TODO is retired.

Related, in the vuln catalog (pfsense-rvb 2026-09-28): the linux `local-user` vuln did
`usermod -a -G sudo`, which fails rc=6 on Fedora (no `sudo` group — RHEL/Fedora use `wheel`).
**Fixed durably in the vulndb `configurations` catalog** (distro-aware `sudo`↔`wheel` map + create
missing groups); the change is recorded in `docs/vulndb-fixes/`. The general lesson for
distro-agnostic vuln curation: never hard-code a distro's admin-group name.

### terraform destroy hangs on a Windows DC with a dead guest-agent (pfsense-rvb 2026-09-28)
*(mitigated 2026-09-29: `destroy-competition.py` pre-stops Windows team clones via a hard API
stop before terraform destroy — an already-stopped box skips the graceful path entirely)*

`terraform destroy` sat "Still destroying… 6m+ elapsed" on a DC. The bpg provider issues a
*graceful* shutdown with a long `timeout_shutdown_vm`, and a DC whose qemu-guest-agent is down
never shuts down, holding the qm lock (so `qm unlock`/`qm stop` also time out). Break it by killing
the qemu process directly: `kill -9 $(cat /var/run/qemu-server/<vmid>.pid)` — terraform then
deletes the stopped VM. The pre-stop covers clones known to teams×boxes naming; anything it
couldn't stop prints the kill -9 recipe.

### Scoring round loop doesn't auto-resume after an engine reboot (pfsense-ad 2026-09-28)
*(detected 2026-09-29: `verify-competition` checks the round loop; `--fix-round-loop` runs the fix)*

After the engine VM rebooted, the scoreboard stayed frozen: `/api/engine` showed a stale
`last_round.StartTime` and a zero `current_round_time`. The Docker containers restart but the round
loop stays stopped. Restart it: `POST /api/competition/start {"started":true}` then `POST
/api/engine/pause {"pause":false}`; a fresh round lands within `Delay` seconds. verify now detects
the stale signature (old StartTime + zero current_round_time, engine unpaused) and warns with those
exact POSTs; `--fix-round-loop` issues them directly. (Symptom trap remains worth knowing:
verify reads the *last scored* round, so a stopped loop reports stale DOWN as if live — the round
check is what disambiguates.)

### In-path pfSense: host-ZFS injection breaks boot; WAN rule needs a keyword (pfsense-ad 2026-09-28)
*(full runbook: `docs/pfsense-inpath-2026-09-28.md`)*

Two traps. (1) Writing the per-team `config.xml` by importing the pfSense ZFS pool on the Proxmox
host makes the pool unmountable by pfSense's FreeBSD loader — `Mounting from zfs:pfSense/ROOT/default
failed with error 22` — because the host's OpenZFS (2.4.4) is newer than the guest's; and renaming
the pool also breaks boot (the loader hardcodes `pfSense`). Inject **guest-side** instead (console
`fetch` over the LAN). (2) `gen_pfsense_config.py` emitted the WAN pass rule with a raw CIDR
`<destination><network>192.168.x.0/24</network>`; pfSense's `<network>` takes a keyword, so it
silently dropped the rule and engine→box scoring timed out. Fixed to `<network>lan</network>`.

### badauto director off red01 silently burns the event; destroy follows config.yaml (pfsense-rvb 2026-09-29)

Two operator traps from the same night, both now guarded in bad-auto (commit 8b93de0):

1. **`python3 -m badauto run` from the dev host does not error.** The loop cycles, the
   scoreboard sensor works (the engine is reachable over mgmt), and every attack tactic
   fails "unreachable over SSH" while LLM spend burns — 26 minutes of fake run and a
   report that looks real (pfsense-rvb session, 01:30-01:56Z). The director's home is
   red01 (`bad-auto deploy --start`); `run --once --dry-run` remains the probe path.
   Fixed: `ensure_on_range` refuses without `/etc/bad-auto/config.yaml` unless
   `BAuto_ALLOW_OFFRANGE=1`, plus a circuit breaker that halts after 8 consecutive
   unreachable attack results (wrong host or route lost mid-event).
2. **`badauto destroy` follows `config.yaml` (`deploy.red_vmid`) with no flags and no
   confirmation.** The README documented a `--competition` flag argparse rejected, and
   config.yaml is machine-written per harness run (run-agent-scrim `stage_red`), so a
   stale config points destroy at whatever VM it names. The regression-4x1 closeout
   worked around exactly this by tearing down manually (beacon STOP + engine NAT
   removal + VM delete) rather than risk the shakedown red01 (999/.244) the config
   named. Fixed: `destroy --competition <dir> --yes` where --competition must MATCH
   config.yaml (mismatch refuses), `--skip-vm` for shared/repurposed red VMs; the
   harness passes both (tezcatlipoca db5a689).
   New facet (same-type-2box closeout, 2026-09-29): **plain `badauto deploy` never
   touches host-side config.yaml** — that's run-agent-scrim's `stage_red` — so after a
   deploy + coverage flow the guard compares against the PREVIOUS run's
   competition_dir. Worse, red vmids recycle: the stale entry named vmid 999, which
   was now the NEW comp's red01 at the same .244. The matching-dir guard can't catch
   this (the identity it checks is itself stale). Before destroying after a plain
   deploy: verify the on-red01 config's `quotient.base_url` names YOUR engine, fix
   config.yaml's `competition_dir` to the live pairing, then `destroy --competition
   <that-dir> --yes`.
   Fixed 2026-09-29 (bad-auto 196846f): deploy stamps red01's identity — VM
   description `bad-auto red01 comp=<name> id=<uuid>` plus a host-side
   `.deploy-stamp.json` — and destroy cross-checks config.yaml, the stamp, and the
   live VM marker before deleting, refusing any disagreement (`--dry-run` rehearses
   all three read-only; `--force` overrides after manual verification). The manual
   recipe above remains the fallback for pre-stamp deployments.

### Two pins of the same check TYPE on one box collapsed to ONE scoreboard check (regression-4x1-2026-09-28) — FIXED 2026-09-29

`box_services.json` pinned apache AND roundcube on the same box and the scoreboard only
registered `web01-http` — `web01-roundcube` never existed (11 checks for 12 pins), so its
bad-auto coverage row could never flip (baseline=0, down=0 forever). Root cause was
**driver-side**: `build_event_conf` deduped checks per box keyed `(check TYPE, port)`, and
apache + roundcube both map to `("Web", 80)` — the second pin was silently dropped at
generate time. The upstream attribution in this entry's first draft was wrong: Quotient's
Box config holds a SLICE per check type (`engine/config/config.go`) and registers every
entry; its only constraint is globally-unique check names built `<box>-<Display>`
(`checks/web.go`), with duplicates a loud config-load error, not a silent drop. svc-matrix
never hit the collapse only because no box carried two `(TYPE, port)`-equal pins. The
bad-auto roundcube→apache2 alias itself is sound; the splunk→lighttpd and telnet→inetd
aliases proved the mechanism live in the same run.

Fixed 2026-09-29 (`build_event_conf` rework + verify `pins_registered` gate): multiple
same-TYPE pins on one box register as separate checks. Identity is `(box, Display)` —
matching Quotient's uniqueness domain — duplicate Displays on a box are rejected at
generate time with the fix in the message, and verify compares the live scoreboard's
ServiceName set against the pins so an unregistered pin FAILs instead of silently
tallying 11-for-12. Dict pins also take per-check overrides (`PIN_CHECK_OVERRIDES`:
display/port/path/scheme/status) so one catalog config can plant once and score twice,
e.g. `{"name": "IIS HTTP", "display": "iis-alt"}`; nakon machine lists strip the
overrides (plants resolve by name+vars). Rule for comp authors: **per box, each pin
needs a distinct scoreboard Display** — same-TYPE pins on one box are fine, but unless
the plant actually gives them distinct ports/processes they still share a failure
domain (stopping apache2 takes every Web vhost on it down at once, which bad-auto's
roundcube→apache2 alias already models).

### shakedown-5x4 event (2026-09-29): the three failed gates and the evidence gaps — all FIXED

The first 4-team event (gates 9/12, [report](shakedown-5x4-2026-09-28-report.md)) failed three
gates for reasons that were all fixable driver/harness defects, not scenario problems:

1. **Blue injects = 0 — inject clocks anchored at phase-7 deploy time.** `resolve_inject_times`
   anchors offsets at phase 7; a `--skip-deploy` restart (or a long staging gap) reaches T0 with
   every inject already expired. This is the same disease as the winad-scrim2 closed-injects
   incident, on the scrim path. FIXED (tezcatlipoca ce491e9): `run-agent-scrim` re-anchors at T0
   unconditionally — `POST /api/injects/{id}` (the engine's UpdateInject; keep-files re-lists
   attachments because unlisted files get deleted) with times recomputed from the comp's
   `injects/*/inject.json` offsets, matched by title. Unordered offsets fail before the event.
   `redeploy --reset-event` (engine re-clone) remains the heavy fallback, no longer required.
2. **Red max_simultaneous_down = 1 + a 315s opening quiet period.** Two bad-auto defects
   (fixed in bad-auto 642675c): (a) the FIRST decision could legally run the full 360 s LLM
   budget before the first attack action — the first decision now gets a 90 s budget
   (`first_decision_budget_sec`) and the deterministic fallback acts on expiry; (b) the loop is
   one-action-per-cycle with a flat 15-min `reimpact_after_min` exclusivity per unit, so blue's
   restorations outpaced re-kills — a detected restore now cancels that unit's exclusivity
   immediately, and while press tempo is active red chains up to `press_impacts_per_cycle` (3)
   additional DISTINCT unit kills in the same cycle (still through validate_decision, with
   per-team headroom tracked between the scoreboard's ~70 s polls).
3. **Teams 3/4 invisible to the evidence loops.** `monitor_loop`, `stage_capture`, the blue
   endpoint/lock selection, `scrim-report`'s `host_label` + `blue_metrics`, and the final-scores
   read all assumed 2 teams — 4-team runs got scoreboard jsonl, final-services JSON, and gate
   counts for teams 1/2 only, and teams 3/4 silently shared team1's LLM endpoint and lock.
   FIXED (ce491e9): everything scales with `--teams` (`_cred_team_names`; per-team
   `--blue{N}-base-url/--blue{N}-model`, one lock per distinct endpoint), and identifiers
   100+i map to team i generically.

Also fixed that night: **teardown destroyed the engine (and its scoring DB) before the report
could read final scores** — `stage_capture` now dumps `evidence/final-scoreboard.json` (teams,
injects, per-team services) BEFORE teardown, and `scrim-report` renders a "Final scores" section
from it; and **bad-auto's report could name boxes that never existed** (a `db01` in the
shakedown report for a range of dc01/win01/web01/app01/edge01) — LLM executive summaries get
phantom box names flagged in place (bad-auto 642675c).

## Known-broken templates

- **`106` / `ubuntu24.04`** and **`920` / `debian13-lite`** (reworded 2026-09-23): these
  templates are **not broken** — they simply ship without cloud-init, so tezcatlipoca clones
  (which rely on cloud-init for network/SSH/user seeding) come up unreachable. For non-driver
  uses (manual VMs, console-worked boxes) they are fine. Use the `-fix` variants for this
  pipeline (see usage-people.md "Adding a template VM"). `deploy()` warns but does not block:
  a reused competition replays its saved `boxes.json` exactly, so the interactive box-picker
  warning can be silently outlived — the deploy-time warning is unconditional (not gated by
  `--yes`) because non-interactive/CI deploys skip the prompts too. Since 2026-09-23 the
  preflight gate additionally verifies every selected template actually resolves to a tagged
  template on the cluster before terraform apply.

## Infrastructure failure modes

- **Fresh clones boot with an empty `/etc/resolv.conf`** (cloud-init ignores `dns.servers` when
  the IP is static), and nakon installs every service with `apt-get`. `fix_dns_on_boxes` exists
  because of a now-deleted helper (`terraform/scripts/prepare_boxes.py`) whose write-up is the
  cautionary tale: nakon reports none of this back — install failures are ignored and the deploy
  never raises — so a box that skips DNS repair installs nothing, Terraform still exits 0, and
  the whole range comes up with every service down.
- **Never point boxes at their own team's `dns*` box from Terraform.** Deadlock: its bind9 is
  installed by nakon, via apt, which needs a resolver that already works — and `fix_dns_on_boxes`
  forces `8.8.8.8` over the top anyway, so the dns* box was never actually serving its team. To
  make a dns* box its team's real resolver, repoint boxes *after* nakon has run.
- **Thick LVM cannot snapshot.** Snapshot-capable datastores: ZFS, LVM-thin, Ceph, or qcow2 on
  file storage. A range deployed on thick LVM has no `tz-base`/`tz-ready` restore points and
  `redeploy-competition.py` falls back to `reconfigure`/`rebuild`.
- **netplan refuses (and warns loudly about) world-readable configs** — the engine's team-NIC
  netplan file is written mode 0600.
- **New VirtIO NICs need a cold boot** — a guest-level reboot does not trigger the PCI scan that
  enumerates them, so the engine's team NICs are addressed only after a hypervisor-level
  stop/start.
- **Quotient crash-loops without `event.conf`, and every container restart drops the team-subnet
  NAT** — hence `event.conf` is pushed before nakon ever runs, and the firewall timer exists.
- **Windows package-manager fallback untested**: nakon's `winget`/`choco` fallback path was never
  exercised by any catalog config used in the verified Windows pass. (E2E #3, 2026-09-23,
  exercises the choco-backed pins — observation recorded in the e2e closeout.)
- **Site-level lab outage (e2e #3, 2026-09-24, ~10 h)**: the whole lab segment vanished from
  off-site while the operator sat on a guest Wi-Fi network — and the tailnet subnet bridge kept
  answering ICMP *for itself* while forwarding nothing, so "ping works" recovered hours before
  any TCP did. Ping is NOT a liveness signal for the node; probe the API port
  (`curl -k https://<node>:8006/` — any HTTP code, not `000`). Deploy state survives such an
  outage: `.deploy_state.json`, snapshots and VM disks are on the node. On recovery, first check
  engine-side nakon tsvs (the remote sweep process may outlive the dead operator SSH), then
  resume `--from-phase` — do not restart the pipeline from scratch.

## Standing limitations

- **Box/credlist usernames are themeable, not secret.** Login and credlist account names default
  to `ubuntu`/`admin`/`user1`/`user2` and can be renamed per competition via
  `competitions/<id>/users.json` — the names aren't secret either way. The *passwords* are always
  generated fresh per competition.
- **Quotient's Postgres/Redis passwords live plaintext on the engine** (`/opt/quotient/.env`),
  generated fresh per deploy by the driver — not in this repo and not from `.env`, but readable
  on the scoring engine by anyone holding it.
- **Windows scoring is port-open only.** Quotient has no native SMB/RDP/WinRM check type, so
  those get a generic `Tcp` dial-and-connect check — it cannot tell a healthy service from one
  that is merely listening.
- **Nakon failures are silent by design.** `install_package` ignores apt exit status and the
  deploy never raises; a mis-seeded box looks exactly like a successful one from the deploy log.
  `verify-competition.py` exists partly because of this. Mitigated 2026-09-23: `run_nakon`
  counts `FAILED` step lines and the tally lands in `.deploy_state.json`
  (`nakon_failed_steps`), which `verify-competition.py` surfaces as a plant-integrity line;
  it also verifies the staged bundle actually landed engine-side before starting a plant
  (the scrim-extreme-2026-09-20 "missing plan archive" class now fails fast with the file
  named).
- **Scrim evictions were not observable** — fixed 2026-09-23: `scrim-report.py` derives
  evictions from red's own health_check chain (each rise in the "N evicted" tally is blue
  removing access red had), counts them in the interaction score and as a red gate, and the
  self-test pin still reproduces the 17c numbers (evictions=0 there).
- **randomize→pin flow drops required catalog vars** (RESOLVED at operator level,
  2026-09-25; catalog-side resolution deferred to upstream nakon/vulndb).
  `nakon randomize` returns vuln names without the vars some catalog configs require, and
  the pinned `box_vulns.json` stores plain strings — so a config like
  `hosts-redirect-linux` (requires `IP`) or `sudoers-rule` (requires a rule) plants rc=2
  ("IP is required"). Surfaced by the strict golden plant (bench-parallel, 2026-09-24: 3
  FAILED steps); the old pipeline carried the same gap. The M4 fix (constants
  `REQUIRED_VARS` + `nakon_ops`): pins accept `{"name": ..., "vars": {...}}`; a bare-name
  pin of a var-requiring config is a **generate-time error** with the exact fix;
  machine-identity vars (`IP`) are auto-filled per machine and **banned from the golden
  stage** (a golden-baked IP would clone into every team — such configs live in
  REPAIR/FINAL_STAGE_CONFIGS, e.g. `hosts-redirect-linux` is now final-stage); and every
  built bundle is **linted** for undeclared `$VAR` references so a catalog config that
  starts needing a var fails at bundle build, not mid-plant (validated against all 123
  existing bundles). `unrealircd-backdoor-container`'s rc=127 is NOT a pin-var problem —
  it assumes docker on the box; it stays documented here and is caught by verify's
  plant-coverage gate when the step fails.

## Security disclosure history

- **2026-08-06 git-history audit**: found a real admin password and team passwords for the CDE
  2026 competition (since torn down) in a pre-2026-07-09 commit predating the
  "stop tracking competition secrets" history rewrite. The commit is no longer reachable from
  any branch but was pushed to GitHub before the rewrite — treat it as permanently public. The
  exposed passwords were rotated and are unused. No real Proxmox API token or nakon/vulndb
  password was found anywhere in this repo's history. A separate `nakon` checkout must audit its
  own history independently.

- **2026-09-29 tracked terraform artifacts audit**: 21 files tracked on the PUBLIC GitHub remote
  carried real per-competition credentials — 9 `terraform.tfvars.json` (`box_password`,
  `teams.teamN.password`), 9 tfstate files (`team_passwords` outputs), and three
  `competitions/m4-validation-2026-09-25/` run logs (`m4-resume-current.log` with three
  `.deploy_state.json` values, plus `m4-scenario8-team-rebuild.log` and `m4-verify-current.log`
  with one each). No standing infrastructure secrets were exposed (no Proxmox API token or current
  `.env` value appears anywhere in the tracked tree), and every affected competition was already
  torn down, so the passwords were operationally dead — nothing to rotate. Resolved by
  curation: the three best-documented competitions keep their terraform artifacts tracked as
  reference (shakedown-5x4-2026-09-28, pfsense-ad-2026-09-27, scrim-extreme-cyberfield-2026-09-22);
  everything else is untracked, and the gitignore now covers the layouts that slipped through
  (comp-dir top-level `*.tfstate*`/`terraform.tfvars*` — exactly how
  `scrim-dress-2026-09-20/terraform.tfstate` got committed — plus `competitions/*/*.log` and
  root `terraform/terraform.tfvars*`). History keeps the removed passwords (same policy as the
  2026-08-06 audit: treat as permanently public; they are dead). Separately, the
  same-type-2box closeout's terraform commit was amended out pre-push — its content never
  reached the remote; 0787d42's rules predate this wider gap closure.

## shakedown-5x4-2026-09-28 (5-box × 4-team on cyberfield/.193)

- **ifupdown2 stale `/run/network/ifstatenew` breaks every network reload** (2026-09-28).
  After team bridges are removed with `ip link del` (instead of `ifdown`), ifupdown2's
  pickled state still lists the deleted bridges; every later `ifreload -a` (including the
  one the terraform proxmox provider runs per bridge create) dies with
  `[Errno 2] ... /sys/class/net/vmbrNNN/brif/` — deploy phase 2 fails on all four bridge
  creates. Fix on the node: `rm /run/network/ifstatenew && ifreload -a`. Rule: tear bridges
  down with `ifdown` (state-updating), not raw `ip link del`. The node's ifstatenew dated
  from the Sep 27 pfsense churn — the rot predated us; our `ip link del` merely exposed it.

- **challenge-* templates occupy vmids 1210–1211** — default team identifiers 101–104
  collide on .193 (team1 = 1210–1214). The vmid-collision preflight gate catches it and
  tells you exactly which vmids; always pass `TF_VAR_team_identifiers` (120–123 →
  1400–1434 is the proven choice) when the node hosts challenge templates.

- **`build_alpine_ci_template.sh` needed five fixes** (shakedown-5x4): (1) the cloud-init
  snippet heredoc was unquoted (`<<YAML`), so backticks in a *comment* ran as command
  substitution; (2) the bootstrap verify used busybox multi-arg `command -v`, which exits 2
  even when everything is installed — the old run "passed" only because nothing read the
  exit code; the gate now checks binaries one `command -v` per item and skips
  `cloud-init status`'s own nonzero exit on "degraded"; (3) cloud-init's `packages:` races
  first-boot network — the full set is re-asserted via `apk add` in runcmd; (4) an unquoted
  YAML scalar containing `NOPASSWD: ALL` parses as a **mapping** (colon-space) and
  cloud-init silently drops the whole runcmd — scalars are quoted now and the snippet
  shape is gated through pyyaml at build time; (5) dl-cdn resolves AAAA and the lab has no
  v6 egress — apk hangs on v6 connects (the "random missing packages per boot" flake);
  bootcmd disables ipv6 (persists to clones, which also fixes the shim's apk).

- **Alpine clones need PERSISTENT `ssh_pwauth` + a `%wheel` sudoers rule in the MAIN
  sudoers file.** The image ships `PasswordAuthentication no` and `ssh_pwauth: false`;
  a clone's first-boot cloud-init re-disables password auth even if the template fixed
  sshd_config, so the golden plant dies with "Bad authentication type". And alpine's sudo
  ships with no `%wheel` rule — the box user's NOPASSWD lives only in sudoers.d, so the
  moment a plant like `writable-sudoers` makes sudoers.d world-writable, sudo ignores the
  entire dir and the pipeline (shim, fix_services, beacons, nakon) loses root. Both are
  baked into the -fix template now (`99-tz-pwauth.cfg`, main-sudoers wheel line).

- **`writable-sudoers` plants sudoers.d as 0777 → sudo refuses the whole dir.** ubuntu/
  fedora survive because medic is in the `sudo`/`wheel` group and nakon authenticates with
  `sudo -S` (password via stdin); alpine needed the template fix above. The **alpine
  service shim now prefers the guest-agent root channel** (ssh+sudo is the fallback);
  `fix_services_on_boxes` already had the same fallback. Anything new that sh-commands
  `sudo` on a post-sweep box must assume password-sudo at best.

- **The `.postclone-swept` marker was not invalidated when a resume re-cloned team boxes**
  (phase 4 re-entry) — FIXED 2026-09-29 (8a83b05): deploy unlinks the marker right after
  apply #2 succeeds, so a following phase-5 always re-sweeps the fresh clones. Hit twice on
  shakedown-5x4; the old workaround (`rm competitions/<id>/.postclone-swept` before any
  `--from-phase 4|5` resume that re-created boxes) is no longer needed.

- **Engine VM DHCP drift + full-content reversion.** The engine ran `ipconfig0 ip=dhcp`;
  an unexplained reboot (during a heavily-loaded churn window) brought it back on a
  different IP (.221→.243→.233) while terraform's saved output — which deploy/verify/
  credentials all consume — stayed stale, and the fresh boot had *lost* the whole
  post-clone system state (no docker, no /opt/quotient, no paramiko; only /opt/nakon from
  the sweeps). Recovery that worked: pin the guest static via netplan +
  `cloud-init network: {config: disabled}` (state output patched to match), then
  `--from-phase 3` to re-prep the engine, `--from-phase 5` after clearing the swept marker
  (clones were re-cloned by the same churn), and re-run the AD-misconfig pass (it only
  runs with first promotion — replayed via
  `domain_ops._run_single_nakon_config` with the 4 AD configs per DC). Both follow-ups
  landed 2026-09-29 (8a83b05): the engine gets a STATIC mgmt IP by default
  (`10.0.0.250`, `TF_VAR_engine_mgmt_ip` override, `''` = DHCP, preflight refuses a
  colliding address), and phase 6 replays the AD plants whenever the
  `.nakon-domain-<team>-ad-misconfigs` marker is missing, even when the domain is up.

- **Freeze gate vs commits:** the frozen record includes the code tree state; committing
  after `--freeze` trips the drift gate on the next deploy ("golden template changed
  since FREEZE: golden_configs"). Freeze LAST, after the final commit; if you must,
  `--unfreeze --confirm-unfreeze` (pre-competition only) and re-freeze.

- **bad-auto red01 bootstrap vs transient pvestatd 596s — FIXED.** The node threw
  HTTP 596 (broken pipe) on `qemu/999/agent/exec` bursts under load ~28, killing bad-auto's
  deploy mid-bootstrap four times; each relaunch got a bit further (idempotent), but a
  wedged apt inside red01 (stale lock from a killed attempt) also needed
  `pkill -9 apt-get; dpkg --configure -a` through the guest agent. Both follow-ups landed:
  agent execs retry through 596/5xx bursts (bad-auto a542df7), and the apt install loop now
  clears a stale dpkg/apt lock (`pkill -9 apt-get` + `dpkg --configure -a`) before each
  retry instead of burning all six attempts on "Could not get lock" (bad-auto 642675c).

- **Node .193 hard-downs are a KNOWN HARDWARE ISSUE of the cyberfield box** (owner
  confirmed 2026-09-29 — not load-related; the shakedown evening hit load 28 with ~27 VMs
  and the node stayed up through all of it). Failure mode observed 2026-09-28: complete
  loss of ping/SSH/API (even .150 on the same lab segment cannot see it) with no
  self-recovery for 1.5+ h; a manual power cycle brought it back, and every running VM
  (the full 27-VM range) came back intact. Plan around it: anything deployed there can
  vanish with the node at any time; recovery = physical power cycle + re-verify (the
  scoring loop needs `POST /api/competition/start` + `/api/engine/pause` after the
  engine reboots).
