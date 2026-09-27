# agent-scrim-2026-09-17 — Competitor Packet

**Difficulty:** 10 / 10

## Scenario

Practice competition for agent blue teams. Meridian Health's regional care network is under a Code Silver cyber incident: the Windows domain controller (dc01, fresh AD forest per team) and its four domain-joined members (win01 ward workstation, web01 patient portal, app01 imaging/DNS, db01 records database) are riddled with Wardline crew persistence - rogue admins, backdoor services, planted credentials, exposed databases, and loosened policies. Hunt down the planted misconfigurations and adversary artifacts while keeping the scored services alive. Two teams defend identical, isolated copies of the network.

## Format

This is an availability-focused defense competition. Each of your team's boxes is polled periodically for the services listed below; keeping them up and reachable scores points over time. Full credentials (team login, box login, and the accounts the scoring checks authenticate with) are issued separately at competition start — this packet only covers what you can prepare for in advance.

### Injects

In addition to uptime scoring, you'll receive timed written taskings ("injects") during the event. Content is released when each one opens; only the schedule is known ahead of time:

| # | Title | Opens | Due | Closes |
|---|---|---|---|---|
| 1 | Code Silver declared -- incident response kickoff | 0m | 20m | 35m |
| 2 | Plaintext credentials found in HR share | 10m | 30m | 50m |
| 3 | Hunt the rogue administrator on the domain controller | 15m | 40m | 1h00m |
| 4 | Persistence sweep: tasks, run keys, and services | 25m | 50m | 1h15m |
| 5 | Firewall anomaly review on Linux wards | 30m | 55m | 1h20m |
| 6 | Outbound beacon and exfil triage | 40m | 1h05m | 1h30m |
| 7 | Domain controller hardening directive | 50m | 1h15m | 1h40m |
| 8 | Draft the patient-data breach notification | 55m | 1h20m | 1h45m |
| 9 | Linux privilege-escalation audit | 1h05m | 1h30m | 1h55m |
| 10 | Insider-threat summary for the CISO | 1h10m | 1h35m | 1h55m |
| 11 | DNS and name-resolution integrity check | 1h20m | 1h45m | 1h58m |
| 12 | Executive incident summary (final deliverable) | 1h30m | 1h52m | 2h00m |

_Offsets are relative to competition start._

## Network layout

Your team is assigned one `/24` subnet, `192.168.<your-team-octet>.0/24` — the same layout for every team, isolated from every other team's. Your systems:

| Hostname | Address | Template/Role |
|---|---|---|
| dc01 | 192.168.X.2 | windows-server-fix |
| win01 | 192.168.X.3 | windows-server-fix |
| web01 | 192.168.X.4 | ubuntu24.04-fix |
| app01 | 192.168.X.5 | debian13-lite-fix |
| db01 | 192.168.X.6 | ubuntu24.04-fix |

_(`X` = your team's assigned octet, given to you at competition start.)_

## Services

Publicly reachable, scored services on each box:

| Hostname | Services |
|---|---|
| dc01 | Enable WinRM |
| win01 | Enable WinRM, New SMB Share, RDP misconfigs |
| web01 | http |
| app01 | dns, http |
| db01 | sql |

## System access

Every box's login account is **`medic`**. Its password, and the separate accounts the scoring checks authenticate with, are issued at competition start — not in this packet.

## Rules of engagement

- Keep your assigned services up and reachable — that's what's scored.
- Don't attack, scan, or otherwise interfere with the scoring engine or any infrastructure outside your team's subnet.
- Don't change the password of any account the scoring checks authenticate with (see System access above) — doing so takes that check down for the rest of the event.
- Direct questions to the event organizers through the announced channel.
