# Insider-threat summary for the CISO

## Findings

- **web01:** non-root UID 0 accounts `backupadmin`, `logviewer`, `monitor`, `svc-health`, `sysmgr`, and `wardline`; all had `/usr/sbin/nologin`. The account names and UID 0 assignment create a direct privilege-escalation and persistence path if credentials or an authorized execution path are obtained.
- **app01:** non-root UID 0 accounts `backupadmin`, `logviewer`, `monitor`, `svc-health`, and `sysmgr`; all had `/usr/sbin/nologin`. These are not required for the scored services and were treated as planted service-style backdoors.
- **db01:** non-root UID 0 accounts `backupadmin`, `logviewer`, `monitor`, `svc-health`, and `sysmgr`; all had `/usr/sbin/nologin`. These accounts were not needed by the SQL service.
- **dc01:** local users `audit_svc`, `wardclerk`, and `svc_sterile` were enabled, passwordless, and service-/role-shaped. `wardclerk` had a recent logon. They were separate from the protected scoring-check account `svc_imaging` and were not required for WinRM.
- **dc01 persistence:** scheduled tasks `DicomsSync` and `LedgerSync` executed PowerShell with `ExecutionPolicy Bypass` from `C:\ProgramData\Wardline\dsync.ps1` and `C:\ProgramData\Wardline\ledger.ps1`. SHA-256 evidence captured before containment: `dsync.ps1` = `7D884795BEE4E557FB770DA16D7E263E3DAB259E38879578D64D70D91E8FB840`; `ledger.ps1` = `64C817C6F2E0C65401ADD23D30F8B719A827242292B381A5FD0DCDFCBFF6783C`.

## Containment

- Locked all listed non-root UID 0 Linux accounts. Their existing nologin shells were retained. The protected operational login `medic` was not changed.
- Removed only the redundant `medic NOPASSWD: ALL` entries from `/etc/sudoers.d/90-cloud-init-users` on web01, app01, and db01. The explicit password-authenticated `medic ALL=(ALL) ALL` rule remains. `visudo -cf /etc/sudoers` parsed successfully on each host.
- Disabled `audit_svc`, `wardclerk`, and `svc_sterile` on dc01. The protected scoring account `svc_imaging` was not changed.
- Disabled `DicomsSync` and `LedgerSync`; no scored service was stopped. WinRM and the other scored checks were rechecked after containment.

## Off-boarding checklist

1. Preserve account metadata, task XML, scripts, hashes, authentication logs, and relevant file timestamps in the incident evidence store.
2. Confirm ownership with clinical IT and service owners before permanent deletion.
3. Rotate any credentials found in task scripts, shares, shell histories, or service definitions; do not rotate scoring-check accounts during the event.
4. Remove disabled backdoor accounts and their profiles after retention approval, then verify group memberships and delegated rights.
5. Review DC, WinRM, Linux auth, sudo, task, and service logs for use of these identities; search for the same indicators across the second team copy.
6. Rebuild or compare against trusted baselines after the competition and enforce least privilege, MFA where available, and monitored change control.

## Residual risk

Evidence was preserved by hashes, but full historical log review and permanent account/script deletion should occur after the event. The protected scoring accounts and availability-critical services remain enabled by design.
