# Meridian Health Executive Incident Summary

**Audience:** Meridian Health Board and executive leadership  
**Reporting point:** T+61 minutes; 29 minutes remained in the exercise

## Executive assessment

The ward network was operating with multiple planted persistence, privilege-escalation,
credential, and name-resolution weaknesses. Blue Team 1 contained the identified
adversary artifacts and hardened the domain controller without changing the three
scoring-check accounts (`triage`, `svc-imaging`, or `wardops`). At the latest check,
all eight scored services were available: HTTP on web01 and app01, DNS on app01,
SQL on db01, WinRM on dc01 and win01, and SMB/RDP on win01.

`web01-http` has lower historical availability because nginx was briefly found
masked/stopped during the persistence sweep; it was unmasked and restarted. Its
current check passes. The other seven services have remained continuously available
in the latest ten-round view.

## Highest-risk findings and actions

1. `lowpriv` had `NOPASSWD: ALL` on web01, app01, and db01. The planted drop-ins
   were removed.
2. `opsuser` had a wildcard `NOPASSWD: /usr/bin/*` rule on all three Linux hosts.
   The rule was removed.
3. `medic` had passwordless unrestricted sudo on all three Linux hosts. Access was
   retained, but sudo now requires the login password.
4. World-writable sudo policy locations and a writable sudo drop-in allowed any
   local user to replace root policy. The planted drop-in was removed, permissions
   were corrected, and `visudo` passed on each host.
5. Planted SUID `find`, editors, and Python provided direct root execution on the
   Linux hosts. The SUID bits were removed and the hosts remained operational.
6. Root-run writable housekeeping persistence existed on web01. The unit was
   disabled and the unit/script permissions were corrected.
7. Persistent Wardline services and scheduled PowerShell actions were present on
   the Windows fleet, including a LocalSystem `WARDLINE` service. Matching tasks
   were removed and the rogue service was stopped and deleted.
8. Windows Run/RunOnce entries and a Linux telemetry service provided persistence
   and possible outbound exfiltration. Matching Run entries and services were
   removed; the suspicious Linux cron item was disabled and retained as evidence.
9. Password and protocol controls on dc01 were weak: minimum length 7, no lockout,
   legacy LM compatibility, optional RDP NLA, and incomplete authentication audit.
   The domain policy, LM level, RDP NLA, SMB1 registry normalization, and logon /
   lockout auditing were hardened and verified.
10. Hosts-file redirects on dc01 and win01 sent Meridian names to
    `198.51.100.77`. The redirects were removed and original files were preserved
    as evidence. The authoritative `meridian.local` zone is also absent from app01,
    which remains a material name-resolution design gap.

## Containment status

The identified planted services, tasks, Run entries, sudo rules, SUID escalation
paths, cron persistence, and hosts-file redirects were removed or disabled. Linux
sudo syntax and BIND configuration validated successfully. WinRM, DNS, HTTP, SQL,
SMB, and RDP checks were revalidated after changes. No reboot was required, and no
scoring-check credentials were changed.

## Residual risk

The missing `meridian.local` authoritative zone and records need reconciliation with
the intended domain DNS design. No guessed records were added during the event because
that could disrupt authentication or scored name resolution. Evidence backups and
disabled artifacts should be retained for incident handling. The earlier nginx
availability interruption reduced historical web01 uptime even though the service is
currently healthy.

## Prioritized remediation roadmap

1. Reconcile and formally deploy the internal DNS zone, records, delegation, and
   resolver policy, then test domain authentication and every application dependency.
2. Rotate any credentials exposed by the HR share, database exposure, planted files,
   or persistence artifacts; review authentication logs for use of those credentials.
3. Centralize endpoint configuration baselines for sudo, SUID inventory, Windows
   services/tasks/Run keys, SMB/RDP, password policy, and audit policy.
4. Add continuous monitoring for unauthorized root-capable policy changes, new
   privileged accounts, persistence creation, hosts-file changes, and unusual egress.
5. Complete a controlled restore and availability test for each scored service, with
   change windows and rollback procedures so hardening cannot interrupt patient care.
