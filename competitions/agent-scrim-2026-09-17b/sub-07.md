# Meridian Health DC Hardening Directive

## Scope and safety

Changes were made on `dc01` only. I did not change passwords for `triage`,
`svc-imaging`/`svc_imaging`, or `wardops`, and I did not restart WinRM or any
scored service.

## Evidence before remediation

- SMB1: `EnableSMB1Protocol=False` (already compliant).
- RDP NLA: `UserAuthentication=0` (not compliant).
- LAN Manager authentication: `LmCompatibilityLevel=1` (legacy setting).
- Password minimum length: 4 or less by the directive's finding; policy was
  below the required hardened value.
- Account lockout: not configured to an effective threshold.
- `auditpol /get /category:*`: logon and account-lockout auditing were `No
  Auditing`.

## Changes applied

- Set minimum password length to `8`.
- Set account lockout threshold to `5` attempts, duration to `15` minutes,
  and observation window to `15` minutes.
- Set RDP `HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp\UserAuthentication` to `1` to require NLA.
- Set `HKLM\SYSTEM\CurrentControlSet\Control\Lsa\LmCompatibilityLevel` to
  `5` to prevent legacy LM/NTLMv1 negotiation.
- Enabled success and failure auditing for `Logon` and `Account Lockout`.

## Verification

- `net accounts`: minimum length `8`; lockout threshold `5`; duration and
  observation window `15` minutes.
- SMB1 remains `False`.
- RDP NLA registry value is `1`.
- LAN Manager compatibility level is `5`.
- `auditpol`: `Logon` and `Account Lockout` both show `Success and Failure`.
- Scored checks remain UP after the changes, including `dc01-winrm`.
