# Domain Controller Hardening Directive

Host: `dc01` (`192.168.102.2`)

## Findings and changes

- Password policy before: minimum length 7; lockout threshold `Never`.
- Set the domain policy with `net accounts` to minimum length 8, lockout threshold 5, lockout duration 30 minutes, and observation window 30 minutes. Post-change verification showed all four values.
- SMB1 protocol was already disabled in the effective SMB server configuration (`EnableSMB1Protocol=False`, SMB2 enabled). A stale `LanmanServer\Parameters\SMB1` registry value was `1`; normalized it to `0` and rechecked it.
- RDP NLA was not required before (`UserAuthentication=0`, `SecurityLayer=2`). Set `UserAuthentication=1`; verified `1` after the change.
- Legacy LM authentication was permitted at level 1. Set `HKLM\SYSTEM\CurrentControlSet\Control\Lsa\LmCompatibilityLevel` to 5; verified 5.
- Basic auditing was absent before. Enabled Success and Failure auditing for `Logon` and `Account Lockout`; `auditpol` verified both settings.

## Availability validation

- `WinRM` service remained `Running`.
- Local TCP test to port 5985 succeeded after changes.
- The scoreboard continued reporting all eight team services UP during this work.
- No reboot was performed and no scoring-check account passwords were changed.
