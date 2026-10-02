# Incident archive — resolved failures, kept for the why

Every entry here is **fixed or mitigated**. It was split out of
[known-issues.md](known-issues.md) on 2026-10-02 (see
[the triage](known-issues-triage-2026-10-02.md)): the write-up is kept because the mitigation's
*rationale* is the load-bearing part, and several mitigations are only as good as that reasoning —
but resolved history does not belong in the list of traps to read before a deploy. For the live trap
list see [e2e-testing.md §0](e2e-testing.md#0-read-before-you-deploy--live-traps); for open problems
see [known-issues.md](known-issues.md).

Entries are in their original order, with dates and commit citations intact. The commit hashes are
the evidence; verify against `git log` before reasoning from them.

### Boot-hostile golden config left every linked clone bootless (2026-09-24)

**Symptom:** every team box of one type came up with no network and no cloud-init after phase 4,
one phase after `POST /template` — a bootless range whose goldens themselves still answered.
**Cause:** `systemd-system-masked` rode the golden disk. Masking multi-user/graphical/default
targets leaves systemd booting with no `multi-user.target`, so sshd/cloud-init never start. The
plant returned rc=0 and the golden's *running* system stayed reachable (masking a target does not
stop the current boot), so `strict=True` saw nothing.
**Fix:** boot-hostile configs were moved into `constants.FINAL_STAGE_CONFIGS`, planted after the
domain joins (`deploy_phases.phase6_domains_and_final`), and `build_golden_set` now **boot-smokes**
each booted golden — one throwaway FULL clone booted to multi-user (guest agent for Windows)
before any conversion, raising on a failed *or unverifiable* smoke so `POST /template` cannot
happen. Cold/unbooted DC goldens skip it deliberately. `golden_boot_smoke 0` in the Compfile
waives the gate and prints that the invariant is UNVERIFIED. Detail:
[benchmark-m02.md](benchmark-m02.md), [internals.md](internals.md#golden_opspy).

### scrim `stage_author` skipped the sweep marker (FIXED 2026-10-01)

**Was:** `run-agent-scrim.py`'s `stage_author` skipped the retired `.phase6-swept` name when
copying a competition template while the pipeline writes `.postclone-swept`, so a leaked sweep
marker was carried into a fresh scrim competition and its post-clone sweep was skipped.
**Fix:** it now skips `.postclone-swept` (the only marker the pipeline writes). Detail:
[scrim-harness.md](scrim-harness.md#run-agent-scrimpy).

### redeploy --reset-event self-deadlock on the phase-7 reseed (FIXED 2026-10-01)

`--reset-event` took the scoring-engine flock, then `reseed_event` spawned
`create-competition --from-phase 7` as a child, whose own `acquire_engine_lock` failed
against its parent's hold — every reset-event died right after the engine rebuild with
"another deploy on this host already holds the scoring-engine lock". Boxes were already
rolled back at that point, so the recovery was manual: re-run the phase-7 reseed by hand.
Fix: `nakon_ops.release_engine_lock()` drops the parent's flock just before the reseed
child takes it (cde-2026 run-2 reset, live-confirmed).


### svc-matrix-2026-09-28: four plant/bring-up defects on the all-services matrix (all FIXED)

The full 16-pin service matrix (every `_SERVICE_TO_CHECK` entry, see the run report
[svc-matrix-2026-09-28-report.md](reports/svc-matrix-2026-09-28-report.md)) surfaced four independent
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

Mitigation (hardening_ops): the SSH script ends with `systemctl reset-failed ssh` + a conditional
start, falling through to the guest-agent fallback when SSH is already gone — either way sshd ends
the pass running.

> **Correction 2026-10-02 (code-verified):** the old claim that "misconfig passes are spaced so
> restarts don't land in the same instant" is **not implemented** — nakon's wrapper inserts no
> inter-step delay and the catalog `ssh-*` script restarts immediately. The protections that
> actually exist are the post-hoc `reset-failed ssh` + conditional start and the guest-agent
> fallback. Do not rely on spacing.

### Clones come up without a routable address (e2e-2026-09-19)

**Symptom:** a clone boots with no routable IPv4 (cloud-init race); ifupdown can also drop the
address on a later carrier blip. Before the fix this surfaced as a phase 5/6 failure an hour after
cloning. **Cause (two deeper ones found 2026-09-24, e2e #3):** (1) a full clone gets a NEW NIC MAC
while cloud-init's `50-cloud-init.yaml` still matches the SOURCE's MAC, so no address ever applies;
(2) `_repair_box_network`'s "first non-loopback interface" selection could pick `docker0`
(web01/db01 run docker), persisting the static config onto the wrong interface. **Fix that survives
(guest-agent exec, no SSH needed):** write `/etc/netplan/99-tz-static.yaml` matching the MAC read
from `/sys/class/net/<if>/address` (no `set-name`, single default route),
`rm -f /etc/systemd/network/90-tz-static.network`, `netplan apply`. Replace the old netplan entirely
if it declares a second default route — `netplan apply` errors on the conflict and applies nothing.
**v2 status:** the mitigation lived in `clone_ops` (`_repair_box_network` / `ensure_cloned_network`),
which was **deleted** as dead code; **there is no v2 equivalent** — a dead clone now surfaces as
`wait_for_boxes_ssh` failing with `diagnose_unreachable_box` output, and the fix is a Terraform
replace or a resume. Linked clones off an already-cleaned golden make the class much rarer, not
impossible.

**Diagnostic detail (still useful if it recurs).** Guest-agent exceptions count as "not yet" so a
merely-slow cloud-init is never "repaired" (that reasoning is worth keeping even without the
helper). The box's NIC can also flap between `eth0`/`ens18` (netplan `set-name` vs predictable
naming), so a correct runtime `ip addr add` gets flushed by a `systemctl restart systemd-networkd`.

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
Terraform therefore runs with `-parallelism=1`.

> **Correction 2026-10-02 (code-verified):** the claims that "the apply timeout scales per Windows
> box (60 GB vs 15 GB Linux images)" and that the 1800 s Proxmox task wait is a terraform setting
> are both wrong. The apply budget is `600 + 300 × <target count>` (`deploy_phases.py`), the 1800 s
> task wait is the bpg provider default, and goldens override it to 5400 s
> (`constants.GOLDEN_CLONE_TIMEOUT`). See [environment-facts.md](environment-facts.md#storage).

### Proxmox source-VM lock race

Proxmox locks the source template during a clone; concurrent clones from the same template race
for the lock and the loser gets a short timeout. Mitigations: every apply runs
`-parallelism=1` (so the pipeline never races itself), and the golden/engine builds are
serialized operator-side. **The old `clone { retries = 15 }` escape hatch is gone** —
`main.tf` no longer sets it, because v2's team boxes are *linked* clones of templates (metadata
work, seconds) rather than full clones of a locked source VM; the remaining full clones (engine
template, goldens) are one-at-a-time by construction. A third-party provisioner cloning the same
source concurrently is still the environmental risk.

### Docker wipes iptables on every start/restart

Docker re-syncs iptables on any container start/restart: `FORWARD` policy goes to `DROP` and the
custom team-subnet MASQUERADE + team-to-team DROP rules vanish. The MASQUERADE loss is silent —
nakon swallows the resulting apt failures (`install_package` ignores exit status), so services
just don't install. Mitigations (engine_ops): `ensure_nat_forwarding` re-asserts both rules
before each nakon pass, `range-firewall.timer` re-asserts them every 30 s for the life of the
range, and `range-healthcheck.timer` logs rule/container status every 60 s.

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

### shakedown-5x4 event (2026-09-29): the three failed gates — all FIXED

The first 4-team event failed three gates for fixable driver/harness reasons, not scenario
problems: (1) **inject clocks anchored at phase-7 deploy time** left every inject expired at T0
(`run-agent-scrim` now re-anchors unconditionally at T0); (2) **red's max_simultaneous_down = 1 plus
a 315 s opening quiet period** (bad-auto 642675c: first-decision budget + restore cancels
re-impact exclusivity); (3) **teams 3/4 invisible to the evidence loops** — `monitor_loop`,
`stage_capture`, the blue endpoint/lock selection, and `scrim-report` all assumed 2 teams (fixed:
everything scales with `--teams`). Also that night: `stage_capture` now dumps
`evidence/final-scoreboard.json` before teardown, and phantom box names in LLM summaries are
flagged in place. Full detail:
[shakedown-5x4-2026-09-28-report.md](reports/shakedown-5x4-2026-09-28-report.md#findings-detail).

### packet-profile validation (cde-2026 on cyberrange/.150, 2026-09-29/30) — all FIXED

The first packet-compiled deploy surfaced nine pipeline failures, all fixed in the same commit. The
shapes (unmanaged boxes vs the golden-hash loop, the static-mgmt-IP gateway default, engine-template
leftover adoption, team identifiers overlapping engine-derived vmid slots, Fedora btrfs root-disk
expansion, Windows account caps vs packet credentials, swallowed domain-chain failures, IIS FTP
enforcing SSL, Windows golden first-boot vs node contention) are reachable without the packet layer,
so the detail is kept in
[packet-validation-cde-2026-2026-09-30.md](reports/packet-validation-cde-2026-2026-09-30.md).

### Scrim evictions were not observable (2026-09-23)

**Was:** `scrim-report.py` could not derive how many footholds blue had removed from red, so
evictions were invisible in the interaction score and as a red gate. **Fixed 2026-09-23:**
`scrim-report.py` derives evictions from red's own `health_check` chain (each rise in the "N
evicted" tally is blue removing access red had), counts them in the interaction score and as a red
gate; the self-test pin still reproduces the 17c numbers (evictions=0 there).
