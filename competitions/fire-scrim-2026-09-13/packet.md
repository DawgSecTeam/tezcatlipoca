# fire-scrim-2026-09-13 — Competitor Packet

**Difficulty:** 10 / 10

## Scenario

Fire scrim for the reigning champion team. Hive Interactive's perimeter collapsed overnight: the Windows domain controller (dc01, fresh AD forest team101.local) and six Linux members are riddled with adversary persistence - rogue admins, weak policies, backdoor services and scheduled tasks, raw-socket beacons, planted credentials, and loosened file permissions. Hunt down the planted misconfigurations and adversary artifacts while keeping the scored services alive.

## Format

This is an availability-focused defense competition. Each of your team's boxes is polled periodically for the services listed below; keeping them up and reachable scores points over time. Full credentials (team login, box login, and the accounts the scoring checks authenticate with) are issued separately at competition start — this packet only covers what you can prepare for in advance.

### Injects

In addition to uptime scoring, you'll receive timed written taskings ("injects") during the event. Content is released when each one opens; only the schedule is known ahead of time:

| # | Title | Opens | Due | Closes |
|---|---|---|---|---|
| 1 | Rogue administrator discovered | 15m | 45m | 1h15m |
| 2 | Credential spill on public share | 45m | 1h30m | 2h00m |
| 3 | Persistence sweep directive | 1h15m | 2h15m | 2h45m |

_Offsets are relative to competition start._

## Network layout

Your team is assigned one `/24` subnet, `192.168.<your-team-octet>.0/24` — the same layout for every team, isolated from every other team's. Your systems:

| Hostname | Address | Template/Role |
|---|---|---|
| dc01 | 192.168.X.2 | windows-server-fix |
| web01 | 192.168.X.3 | ubuntu24.04-fix |
| app01 | 192.168.X.4 | ubuntu24.04-fix |
| db01 | 192.168.X.5 | debian13-lite-fix |
| mail01 | 192.168.X.6 | ubuntu24.04-fix |
| files01 | 192.168.X.7 | debian13-lite-fix |
| monitor01 | 192.168.X.8 | ubuntu24.04-fix |

_(`X` = your team's assigned octet, given to you at competition start.)_

## Services

Publicly reachable, scored services on each box:

| Hostname | Services |
|---|---|
| dc01 | Enable WinRM |
| web01 | http, imap |
| app01 | dns, http |
| db01 | sql |
| mail01 | imap, smtp |
| files01 | ftp, telnet |
| monitor01 | dns, http |

## System access

Every box's login account is **`ubuntu`**. Its password, and the separate accounts the scoring checks authenticate with, are issued at competition start — not in this packet.

## Rules of engagement

- Keep your assigned services up and reachable — that's what's scored.
- Don't attack, scan, or otherwise interfere with the scoring engine or any infrastructure outside your team's subnet.
- Don't change the password of any account the scoring checks authenticate with (see System access above) — doing so takes that check down for the rest of the event.
- Direct questions to the event organizers through the announced channel.
