# ui-duo-clean — Competitor Packet

**Difficulty:** 2 / 10

## Scenario

Meridian Components runs exactly two public-facing systems: lnx01, a Linux build-artifacts server publishing through nginx, and win01, a standalone Windows reporting host serving IIS. There is no Active Directory — each host stands alone with local accounts only. Nothing on either host is deliberately broken or backdoored. As the blue team, keep both scored services available for the whole event; every outage you cause is on you.

## Format

This is an availability-focused defense competition. Each of your team's boxes is polled periodically for the services listed below; keeping them up and reachable scores points over time. Full credentials (team login, box login, and the accounts the scoring checks authenticate with) are issued separately at competition start — this packet only covers what you can prepare for in advance.

### Injects

In addition to uptime scoring, you'll receive timed written taskings ("injects") during the event. Content is released when each one opens; only the schedule is known ahead of time:

| # | Title | Opens | Due | Closes |
|---|---|---|---|---|
| 1 | Service Baseline Report | 0m | 72h00m | 168h00m |
| 2 | Hardening Pass — Zero Outage | 0m | 168h00m | 336h00m |

_Offsets are relative to competition start._

## Network layout

Your team is assigned one `/24` subnet, `192.168.<your-team-octet>.0/24` — the same layout for every team, isolated from every other team's. Your systems:

| Hostname | Address | Template/Role |
|---|---|---|
| lnx01 | 192.168.X.2 | base-ubuntu24.04-fix |
| win01 | 192.168.X.3 | base-windows-server |

_(`X` = your team's assigned octet, given to you at competition start.)_

## Services

Publicly reachable, scored services on each box:

| Hostname | Services |
|---|---|
| lnx01 | http |
| win01 | IIS HTTP |

## System access

Every box's login account is **`ubuntu`**. Its password, and the separate accounts the scoring checks authenticate with, are issued at competition start — not in this packet.

## Rules of engagement

- Keep your assigned services up and reachable — that's what's scored.
- Don't attack, scan, or otherwise interfere with the scoring engine or any infrastructure outside your team's subnet.
- Don't change the password of any account the scoring checks authenticate with (see System access above) — doing so takes that check down for the rest of the event.
- Direct questions to the event organizers through the announced channel.
