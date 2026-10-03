# Insider-threat summary for the CISO

## Findings and containment

- **dc01:** `audit_svc`, `wardclerk`, and `svc_sterile` were enabled, passwordless, and shaped like service or role accounts. `wardclerk` had a recent logon at approximately 08:30Z. These accounts were not required for WinRM and were disabled. Protected `svc_imaging` was not changed.
- **win01:** `nightnurse`, `pharmtech`, `svc_biomed`, and `wardvol` were enabled, passwordless, human- or service-shaped local accounts. They were not members of the local Administrators group, but they provide unnecessary footholds and were disabled. The local Administrator and domain-admin access used for operations were not changed.
- **web01, app01, db01:** current account inventory showed only `root` with UID 0; no non-root UID-0 backdoor accounts remained. The existing `sudo` groups retain operational access for `medic`; no scoring account was modified.

An attacker with credentials for the disabled accounts could use them for persistence, interactive access, or to blend into clinical/service activity. Disabled accounts should be retained as evidence until ownership and log review are complete.

## Off-boarding checklist

1. Preserve account metadata, group membership, last-logon data, task/service definitions, and authentication logs.
2. Confirm ownership with clinical IT and service owners before deleting accounts or profiles.
3. Rotate credentials found in shares, scripts, task definitions, service configurations, or shell history; do not rotate `triage`, `svc-imaging`, or `wardops` during the event.
4. Review DC, WinRM, Linux auth, sudo, task, and service logs for use of these identities and search the second team copy for the same indicators.
5. Remove disabled accounts and profiles after retention approval, then verify privileged groups, delegated rights, scheduled tasks, services, and run keys.
6. Rebuild or compare hosts against a trusted baseline and add monitoring for new privileged accounts and persistence.

## Residual risk

Historical log review and permanent deletion remain post-event work. The protected scoring accounts and availability-critical services remain enabled by design. After containment, all eight scored services were still passing, including `dc01-winrm`, `win01-winrm`, `win01-smb`, and `win01-rdp`.
