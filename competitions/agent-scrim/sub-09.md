# Linux privilege-escalation audit

Scope: `web01` (192.168.101.4), `app01` (192.168.101.5), and `db01`
(192.168.101.6). Evidence was collected with root-readable metadata and service
configuration checks. No scored service or scoring-check account was changed.

| Host | Finding | Escalation path / assessment | Action |
|---|---|---|---|
| web01, app01, db01 | `/etc/sudoers.d` was mode 777 and `zz-writable` was mode 666 | Any local user could alter sudo policy and obtain root; planted | Removed `zz-writable`; changed directory to 755; validated with `visudo` |
| web01, app01, db01 | `lowpriv ALL=(ALL) NOPASSWD: ALL` | `lowpriv` could run any command as root without a password; planted | Removed the drop-in |
| web01, app01, db01 | `opsuser ALL=(ALL) NOPASSWD: /usr/bin/*` | `opsuser` could use permitted interpreters/utilities for root escalation; planted wildcard | Removed the drop-in |
| web01, app01, db01 | `medic ALL=(ALL) NOPASSWD: ALL` | Compromise of the operational login immediately became root; planted policy | Retained `medic` access but changed it to password-required `ALL` |
| web01 | SUID `find` (mode 6755) | SUID `find` can execute a root shell/commands; planted | Removed SUID bit |
| web01 | SUID `nano` and `vim.basic` (mode 6755) | SUID editors can write arbitrary root-owned files or spawn root shells; planted | Removed SUID bits |
| app01 | SUID `find` (mode 6755) | SUID command execution provides direct root escalation; planted | Removed SUID bit |
| db01 | SUID `/usr/bin/python3.12` (mode 6755) | SUID interpreter provides arbitrary root code execution; planted | Removed SUID bit |
| web01 | `/etc/systemd/system/housekeeping.service` and `/opt/scripts/housekeeping.sh` were writable (777) | A local user could replace a root-run service script; planted persistence/escalation | Disabled the unit; changed unit to 644 and script to 750 |
| web01 | Enabled `portal-healthd.service` contained `EXFIL_URL=http://feed.wardline-telemetry.net/collect` | Root-restarted telemetry persistence could beacon/exfiltrate; planted | Disabled/stopped the unit; preserved unit for evidence |
| app01 | Enabled/active `WardOptimizer.service` | Suspicious persistent system service with no operational dependency; planted | Disabled/stopped the unit |
| app01 | Enabled/active `dns-cache-sync.service` | Suspicious persistent resolver helper; not required by scored DNS check; planted | Disabled/stopped the unit |
| db01 | `/etc/cron.d/cocoa-hidden-sync` ran `/opt/.cocoa/cocoa-sync` as root every 15 minutes | Hidden root cron persistence; planted | Moved to `/root/cocoa-hidden-sync.disabled` as evidence |

## Validation

- `visudo -cf /etc/sudoers` passed on all three hosts.
- SSH remained usable with `medic` and password-authenticated sudo.
- Scored services remained UP: `app01-http`, `app01-dns`, `web01-http`,
  `db01-sql`, `dc01-winrm`, `win01-winrm`, `win01-smb`, and `win01-rdp`.
- No scoring-check accounts (`triage`, `svc-imaging`, `wardops`) were modified.
