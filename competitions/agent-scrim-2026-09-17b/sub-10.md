# Inject 10: Insider-threat summary for the CISO

## Executive assessment

The fleet contained multiple human-shaped backdoors with root or local-administrator capability. These accounts would let an attacker bypass normal service boundaries, persist across reboots, and blend into daemon/service names. The review preserved the approved management path and did not alter the protected scoring accounts `triage`, `svc-imaging`, or `wardops`.

## Evidence and disposition

| Host | Account(s) | Evidence / attacker use | Disposition |
|---|---|---|---|
| web01 | `backupadmin`, `logviewer`, `monitor`, `svc-health`, `sysmgr`, `wardline` | UID 0 with interactive shells; any successful login would be equivalent to root | Flagged for controlled disable/delete after ownership validation; `wardline` is inconsistent with a legitimate daemon identity |
| app01 | `backupadmin`, `logviewer`, `monitor`, `svc-health`, `sysmgr` | UID 0 with interactive shells; direct root persistence | Flagged for controlled disable/delete after ownership validation |
| db01 | `backupadmin`, `logviewer`, `monitor`, `svc-health`, `sysmgr`, `wardline` | UID 0 with interactive shells; direct root persistence | Flagged for controlled disable/delete after ownership validation |
| dc01 | `transit` | Unexpected member of local `Administrators`; enables full local takeover | Flagged for removal after confirming no approved operational dependency |
| dc01 | `svc_imaging` | Administrative membership observed, but this is a protected scoring-check account | Left unchanged per rules of engagement |

The Linux review also found suspicious privileged-group memberships: `vendor-support` on web01/app01/db01, `imaging-ops` on app01, and `wardline` on db01. These memberships create a second path to root and should be removed after validating any legitimate support dependency. Earlier persistence and privilege-escalation remediation removed planted broad sudo rules, writable service/cron paths, and SUID escalation bits while keeping scored services available.

## Recommended off-boarding checklist

1. Confirm ownership and service dependencies from package, systemd, scheduled-task, and authentication logs.
2. Disable suspicious accounts first, set non-login shells, revoke group memberships, and invalidate active sessions/keys.
3. Preserve account metadata, sudo/group files, and relevant logs as evidence before deletion.
4. Delete only after validation; rotate any credentials found in account homes, scripts, or shares.
5. Review domain and local Administrator memberships, password age/expiry, MFA coverage, and new-account events.
6. Recheck WinRM, SMB, RDP, HTTP, DNS, and SQL scoring paths after each change.

## Residual risk

The identities listed above need an owner/service-dependency decision before destructive removal. Until then, they remain potential root or administrator access paths. No protected scoring account was changed and current availability checks were passing at review time.
