# Inject 9: Linux privilege-escalation audit

Scope: `web01` (192.168.101.4), `app01` (192.168.101.5), and `db01` (192.168.101.6). Evidence was collected with `find`, `getent`, `sudo -l`, `visudo -c`, and service status checks. Scored services remained available throughout.

## Findings and actions

| Host | Finding | Escalation path / assessment | Action |
|---|---|---|---|
| web01 | SUID `find` | `find -exec` could execute commands as root; planted | Removed SUID bit; preserved finding in the audit log |
| web01 | SUID `nano` | SUID editor could write arbitrary root files; planted | Removed SUID bit |
| web01 | SUID `vim.basic` | SUID editor could spawn a root shell/write root files; planted | Removed SUID bit |
| web01 | `/etc/crontab` mode 0666 | Any local user could add a root cron command; planted | Changed to 0644 |
| web01 | `housekeeping.service` mode 0777 | Any local user could replace a root-run unit; planted | Changed to 0644; disabled the non-scored oneshot |
| web01 | `/opt/scripts/housekeeping.sh` mode 0777 | Any local user could replace the root-run service payload; planted | Changed to 0755 |
| web01 | `/etc/sudoers.d` mode 0777 | Users could create sudo policy files; planted | Changed to 0755 |
| web01 | `lowpriv ALL=(ALL) NOPASSWD: ALL` | Any `lowpriv` shell could become root; planted | Backed up under `/root/ir-evidence-09/`, then removed |
| web01 | `opsuser` wildcard sudo rule | `opsuser` could run arbitrary `/usr/bin` programs as root; planted | Backed up, then removed |
| web01 | `Defaults timestamp_timeout=-1` | Stolen sudo authentication could remain valid indefinitely; planted | Backed up, then removed |
| app01 | SUID `find` | `find -exec` root command execution; planted | Removed SUID bit |
| app01 | `/etc/crontab` mode 0666 | Local user to root via cron edit; planted | Changed to 0644 |
| app01 | `housekeeping.service` and payload modes 0777 | Replace root-run service/unit; planted | Changed unit to 0644 and payload to 0755; disabled service |
| app01 | `/etc/sudoers.d` and `zz-writable` writable | Inject/modify sudo policy; planted | Directory changed to 0755; file changed to 0440 |
| app01 | `lowpriv` NOPASSWD rule | Direct root sudo; planted | Backed up and removed |
| app01 | `opsuser` `/usr/bin/*` wildcard | Arbitrary root command via permitted binaries; planted | Backed up and removed |
| db01 | SUID `find` | `find -exec` root command execution; planted | Removed SUID bit |
| db01 | SUID `python3.12` | SUID interpreter could execute arbitrary root Python; planted | Removed SUID bit |
| db01 | SUID `vim.basic` | SUID editor root shell/file-write path; planted | Removed SUID bit |
| db01 | `/etc/crontab` mode 0666 | Local user to root via cron edit; planted | Changed to 0644 |
| db01 | `housekeeping.service` and payload modes 0777 | Replace root-run service/unit; planted | Changed unit to 0644 and payload to 0755; disabled service |
| db01 | `lowpriv`/`opsuser` broad sudo rules | Direct or indirect root execution; planted | Backed up and removed |

## UID 0 review

The UID 0 inventory included `root` plus suspicious interactive accounts such as `backupadmin`, `logviewer`, `monitor`, `svc-health`, `sysmgr`, and `wardline` on the Linux wards. These were not changed in this pass because they need service-ownership validation before account locking; they are escalation findings for follow-up. Protected scoring-check accounts (`triage`, `svc-imaging`, and `wardops`) were not changed.

The remaining `medic` access rule was left in place to preserve the approved management path. `visudo -c` passed on all three hosts. Final availability check: 8/8 scored services UP, including web01 HTTP, app01 HTTP/DNS, db01 SQL, and all Windows checks.
