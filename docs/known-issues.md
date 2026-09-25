# Known issues & incident log

Live-confirmed incidents, standing failure modes, and limitations of this range — the things
that have actually burned a deploy or a scrim. Design consequences live in
[internals.md](internals.md) (deploy pipeline) and [scrim-harness.md](scrim-harness.md) (agent
harness); symptom-level fixes live in [usage-people.md](usage-people.md)'s Troubleshooting table.

## Live-confirmed incidents

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
*(root cause UNSOLVED — workaround: rebuild the box, or never restart a planted one)*

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
to bisect. A bisect that survives the next outage window is the standing follow-up.

### Phase-6 mid-crash resume is safe; Windows-clone bootstrap needs the agent up (~8 min) (e2e #3, 2026-09-24)

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

## Security disclosure history

- **2026-08-06 git-history audit**: found a real admin password and team passwords for the CDE
  2026 competition (since torn down) in a pre-2026-07-09 commit predating the
  "stop tracking competition secrets" history rewrite. The commit is no longer reachable from
  any branch but was pushed to GitHub before the rewrite — treat it as permanently public. The
  exposed passwords were rotated and are unused. No real Proxmox API token or nakon/vulndb
  password was found anywhere in this repo's history. A separate `nakon` checkout must audit its
  own history independently.
