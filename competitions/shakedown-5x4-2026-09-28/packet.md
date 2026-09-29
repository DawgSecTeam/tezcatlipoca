# shakedown-5x4-2026-09-28 — Competitor Packet

**Difficulty:** 5 / 10

## Scenario

Helios Robotics regional engineering site: an Active Directory domain fronted by a domain controller (dc01) with a domain-joined Windows application and file server (win01), an Ubuntu public web tier (web01), a Fedora build and internal-DNS server (app01), and an Alpine Linux edge cache appliance (edge01). An intrusion crew has been quiet-staging across the site for weeks; blue teams keep the ten scored services alive, hunt planted persistence, and work injects while an automated red operator presses every team simultaneously.

## Format

This is an availability-focused defense competition. Each of your team's boxes is polled periodically for the services listed below; keeping them up and reachable scores points over time. Full credentials (team login, box login, and the accounts the scoring checks authenticate with) are issued separately at competition start — this packet only covers what you can prepare for in advance.

### Injects

In addition to uptime scoring, you'll receive timed written taskings ("injects") during the event. Content is released when each one opens; only the schedule is known ahead of time:

| # | Title | Opens | Due | Closes |
|---|---|---|---|---|
| 1 | Helios IR activation -- kickoff and baseline | 0m | 20m | 35m |
| 2 | Credential spill on the build network | 30m | 55m | 1h15m |
| 3 | Rogue admin hunt in the directory | 1h10m | 1h40m | 2h00m |
| 4 | Persistence sweep across all systems | 1h50m | 2h20m | 2h40m |
| 5 | Executive incident summary due | 2h30m | 2h50m | 3h00m |

_Offsets are relative to competition start._

## Network layout

Your team is assigned one `/24` subnet, `192.168.<your-team-octet>.0/24` — the same layout for every team, isolated from every other team's. Your systems:

| Hostname | Address | Template/Role |
|---|---|---|
| dc01 | 192.168.X.2 | base-windows-server |
| win01 | 192.168.X.3 | base-windows-server |
| web01 | 192.168.X.4 | base-ubuntu24.04-fix |
| app01 | 192.168.X.5 | base-fedora44-fix |
| edge01 | 192.168.X.6 | base-alpine3.23-fix |

_(`X` = your team's assigned octet, given to you at competition start.)_

## Services

Publicly reachable, scored services on each box:

| Hostname | Services |
|---|---|
| dc01 | ADDS, Enable WinRM |
| win01 | IIS HTTP, New SMB Share |
| web01 | http, ssh |
| app01 | dns, http |
| edge01 | http, ssh |

## System access

Every box's login account is **`medic`**. Its password, and the separate accounts the scoring checks authenticate with, are issued at competition start — not in this packet.

## Rules of engagement

- Keep your assigned services up and reachable — that's what's scored.
- Don't attack, scan, or otherwise interfere with the scoring engine or any infrastructure outside your team's subnet.
- Don't change the password of any account the scoring checks authenticate with (see System access above) — doing so takes that check down for the rest of the event.
- Direct questions to the event organizers through the announced channel.
