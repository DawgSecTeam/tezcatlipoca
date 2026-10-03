# amongus-cde-2026 — Competitor Packet

**Difficulty:** 6 / 10

## Scenario

The Skeld CDE: mira (Windows Server 2019 DC, forest team<id>.local — the CDE spec's mira-01.corp.sus theme) with a deliberately wide-open user fleet and guest admin; skeld (IIS FTP + wide-open C share) carries the inject drop; airship (CentOS 8) serves the crew web app through Apache against polus's MariaDB. Defend the crew: keep scored services up while hunting the planted misconfigurations.

## Format

This is an availability-focused defense competition. Each of your team's boxes is polled periodically for the services listed below; keeping them up and reachable scores points over time. Full credentials (team login, box login, and the accounts the scoring checks authenticate with) are issued separately at competition start — this packet only covers what you can prepare for in advance.

### Injects

In addition to uptime scoring, you'll receive timed written taskings ("injects") during the event. Content is released when each one opens; only the schedule is known ahead of time:

| # | Title | Opens | Due | Closes |
|---|---|---|---|---|
| 1 | Crew Tasking — Baseline Security Audit | 0m | 1h00m | 1h30m |
| 2 | Imposter Activity — Containment Order | 30m | 2h00m | 3h00m |

_Offsets are relative to competition start._

## Network layout

Your team is assigned one `/24` subnet, `192.168.<your-team-octet>.0/24` — the same layout for every team, isolated from every other team's. Your systems:

| Hostname | Address | Template/Role |
|---|---|---|
| mira | 192.168.X.2 | base-windows-server-2019 |
| skeld | 192.168.X.3 | base-windows-server |
| airship | 192.168.X.4 | base-centos8-fix |
| polus | 192.168.X.5 | base-ubuntu20.04-fix |

_(`X` = your team's assigned octet, given to you at competition start.)_

## Services

Publicly reachable, scored services on each box:

| Hostname | Services |
|---|---|
| mira | Enable WinRM, New SMB Share, ad-dns-localhost |
| skeld | IIS FTP, New SMB Share |
| airship | http, ssh |
| polus | sql, ssh |

## System access

Every box's login account is **`blackteam`**. Its password, and the separate accounts the scoring checks authenticate with, are issued at competition start — not in this packet.

## Rules of engagement

- Keep your assigned services up and reachable — that's what's scored.
- Don't attack, scan, or otherwise interfere with the scoring engine or any infrastructure outside your team's subnet.
- Don't change the password of any account the scoring checks authenticate with (see System access above) — doing so takes that check down for the rest of the event.
- Direct questions to the event organizers through the announced channel.
