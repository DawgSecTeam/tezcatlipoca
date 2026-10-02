# PAM account-stage SSH denial after a restart (e2e #3, 2026-09-24) — UNRESOLVED

Full evidence record for the one incident in this range whose root cause is still unknown. Split out
of [known-issues.md](../known-issues.md) on 2026-10-02 so the open list stays readable; the summary
and current status live there. **This file is the only record of the diagnostic kit — do not prune it
without rebuilding the lab.**

**Status:** OPEN — root cause unsolved. Workaround: rebuild the box (`redeploy --mode rebuild`), or
never restart a planted box. A 2026-09-29 research pass could not reproduce it from the visible pin
set, and the standing lab (vmid 1181 `tzlive-pamlab`) was destroyed in the same day's range cleanup,
so the incident is currently **unreproducible from the repo** — the next action is to rebuild the lab
as a committed script.

### Planted Linux boxes deny all SSH at PAM account stage after a restart (e2e #3, 2026-09-24)
*(root cause UNSOLVED — workaround: rebuild the box, or never restart a planted one.
2026-09-29 research pass: the visible pin set does NOT reproduce it; the prior session's
bisect results recovered; the pam-lab built for it (vmid 1181) was destroyed in the same
day's cleanup — rebuild from a base-ubuntu24.04-fix clone + the plant rungs below)*

**Symptom:** phase-5-planted team1 Linux boxes (web01, db01, app01 in its first life) stop accepting
SSH entirely after their next stop/start — the TCP handshake completes, the connection dies at
preauth. paramiko reports "Authentication failed: transport shut down or saw EOF"; sshd logs
`fatal: Access denied for user <u> by PAM account configuration [preauth]`; the audit record says
`op=PAM:accounting grantors=?`.
**Cause:** unknown, and disk-persistent but boot-triggered — the box's sshd works immediately after
its plant and breaks on the next boot. Everything that could explain it verifies clean (sshd binary
and config via `dpkg -V`/`sshd -t` rc=0, PAM modules, stock `common-account`, no nologin files,
valid shadow, `unix_chkpwd <u> chkexpiry` rc=0 for every user); strace shows pam_unix read the whole
shadow and then fail with no syscall in between.
**Fix:** none known. A full disk rebuild (`redeploy --mode rebuild`) fixes it, so the workaround is
rebuild, or never restart a planted box. The planted backdoors (`auth sufficient pam_permit.so`
prepended to `common-auth`, su's `[success=done] pam_permit`) are scoring vulns, NOT the cause.

The 2026-09-29 research pass and the diagnostic kit follow — this is the only record of an
unresolved incident, so the evidence is kept verbatim.

**Diagnostic kit** (all guest-agent-driven, since SSH is the thing that's broken): agent-exec
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

