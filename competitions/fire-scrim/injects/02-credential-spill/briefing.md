# Inject 2 — Credential spill on public share

**Priority: HIGH — due in 45 minutes**

An employee reported that a file containing payroll service credentials is readable by
everyone through a departmental SMB share on `dc01`, and a second credentials file is sitting
world-readable in `/opt/hive/` on `db01`.

Your tasks:

1. Remove the payroll credentials file from the SMB share and the world-readable file on
   `db01`; rotate the credentials they reference.
2. Tighten the share and filesystem permissions so only the responsible service accounts
   can read them.
3. Check whether the spilled credentials were used from anywhere (logon events, sudo logs,
   database connections) and report findings.

Scoring: service uptime continues throughout; inject score is awarded on reviewer evaluation
after close of business.
