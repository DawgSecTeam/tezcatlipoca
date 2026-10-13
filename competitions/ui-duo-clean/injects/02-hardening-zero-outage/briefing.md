# Hardening Pass — Zero Outage

From: Meridian Components — IT Director
To: Infrastructure Response Team

Auditors are on-site next week. Apply sensible hardening to both hosts **without** interrupting
the scored services. Availability is being scored continuously — a hardened host that is down
scores worse than an unhardened host that is up.

## Suggested items

- lnx01: patch the OS, restrict SSH (key-only, no root), tighten the nginx config, enable and
  review logs.
- win01: patch the OS, review local administrators, confirm the host firewall only admits what
  IIS and RDP need, enable auditing.
- Both: document what you changed and how you would roll it back.

## Constraints

- Scored checks hit nginx and IIS on port 80 from the scoring engine throughout the event.
- Breaking a scored service costs points every round it stays down. Roll back anything that does.
