# scrim-reset — Competitor Packet

**Difficulty:** 5 / 10

## Scenario

MIRA Corp incident response (Among Us themed): investigate the Imposter activity on MIRA's systems while defending them and upholding business operations.

## Format

This is an availability-focused defense competition. Each of your team's boxes is polled periodically for the services listed below; keeping them up and reachable scores points over time. Full credentials (team login, box login, and the accounts the scoring checks authenticate with) are issued separately at competition start — this packet only covers what you can prepare for in advance.

### Injects

In addition to uptime scoring, you'll receive timed written taskings ("injects") during the event. Content is released when each one opens; only the schedule is known ahead of time:

| # | Title | Opens | Due | Closes |
|---|---|---|---|---|
| 1 | CDE 2026 — Welcome, crew check-in | 0m | 30m | 1h00m |

_Offsets are relative to competition start._

## Network layout

Your team is assigned one `/24` subnet, `192.168.<your-team-octet>.0/24` — the same layout for every team, isolated from every other team's. Your systems:

| Hostname | Address | Template/Role |
|---|---|---|
| ad01 | 192.168.X.2 | base-windows-server |
| ftp01 | 192.168.X.3 | base-windows-server |
| web01 | 192.168.X.4 | base-fedora44-fix |
| db01 | 192.168.X.5 | base-ubuntu24.04-fix |

_(`X` = your team's assigned octet, given to you at competition start.)_

## Services

Publicly reachable, scored services on each box:

| Hostname | Services |
|---|---|
| ad01 | dns, ldap, smb |
| ftp01 | IIS FTP, ftp, smb |
| web01 | http, ssh |
| db01 | sql, ssh |

## System access

Every box's login account is **`blueteam`**. Its password, and the separate accounts the scoring checks authenticate with, are issued at competition start — not in this packet.

## Rules of engagement

- Keep your assigned services up and reachable — that's what's scored.
- Don't attack, scan, or otherwise interfere with the scoring engine or any infrastructure outside your team's subnet.
- Don't change the password of any account the scoring checks authenticate with (see System access above) — doing so takes that check down for the rest of the event.
- Direct questions to the event organizers through the announced channel.
