# Packet fidelity — CDE 2026

Compiled from `packets/cde-2026/packet.yaml` (packet: CDE 2026 Blue Team Packet v1.0 (Among Us / MIRA Corp))

Statuses: **exact** = delivered as promised · **substituted** = equivalent capability, different implementation · **unsupported** = promised shape the pipeline cannot express (documented, not silently dropped).

## Boxes

| Box | Packet says | Built as | Status | Note |
|---|---|---|---|---|
| fw01 | pfSense firewall — 192.168.[Team#].1, mgmt SSH 22 / web 80+443, 0 scored services | `pfsense` (unmanaged) | substituted | In-path pfSense owns .1 as teams' gateway (matches the packet) but the wiring is the manual runbook (docs/pfsense-inpath-2026-09-28.md), not terraform. For a validation deploy the box can sit idle while the engine holds .1; wire it in-path before the real event. |
| ad01 | Windows Server 2019 — ADDS, LDAP 389 / DNS 53 / SMB 445 | `base-windows-server` | substituted | 2022-eval lineage template, not 2019; ADDS/DNS/SMB all present |
| ftp01 | Windows Server 2022 — FTP server, FTP 20/21 / SMB 445 | `base-windows-server` | substituted | 2022-eval lineage (same base as ad01, distinct box/SID); IIS FTP + SMB share planted |
| web01 | CentOS 8 — Apache ecommerce web page, HTTP 80 / SSH 22 | `base-fedora44-fix` | substituted | no CentOS 8 template; Fedora 44 is the proven dnf-family stand-in (apache+bind live-validated) |
| db01 | Ubuntu 20.04 — O2 control systems, MySQL (MariaDB) 3306 / SSH 22 | `base-ubuntu24.04-fix` | substituted | no 20.04 template with working cloud-init; 24.04 carries MariaDB identically |

## Scored services

| Box | Packet service | Port | Check | Status | Note |
|---|---|---|---|---|---|
| ad01 | LDAP for directory services | 389 | `ADDS` | exact | Tcp port-open check — Windows scoring is port-open-only (known-issues standing limit) |
| ad01 | DNS for domain name resolution | 53 | score-only tcp | substituted | native AD DNS needs no plant; port-open check, not a protocol-aware DNS query |
| ad01 | SMB for filesharing (AD SYSVOL/NETLOGON) | 445 | score-only tcp | substituted | native AD share; port-open check |
| ftp01 | FTP server for file transfer and storage (site) | 21 | `IIS FTP` | exact | plants the IIS FTP site (teams log in with the packet's local account exactly like the real event); scored by the port-open companion below |
| ftp01 | FTP server for file transfer and storage | 21 | score-only tcp | substituted | live validation 2026-09-30: the IIS FTP site enforces SSL (534) — the engine's plain-FTP check cannot authenticate against it, so the auth dimension collapses to a port-open check over the planted site |
| ftp01 | SMB for filesharing (planted share) | 445 | `New SMB Share` | exact | same-TYPE-as-ad01 pin scores separately per box (unique Display rule) |
| web01 | Ecommerce web page (Apache) | 80 | `apache` | exact | Web check: GET / must answer 200 |
| web01 | System management SSH | 22 | `ssh` | substituted | local-credlist check. The packet's domain dimension is NOT expressible here (live validation 2026-09-30): the local blueteam (n0t_sus1) shadows the domain blueteam of the same name (NSS resolves files before sssd), and the engine's shared credlist cannot carry per-team qualified names (blueteam@mira-120 vs mira-121). Single check scores the service; credlists match the packet. |
| db01 | MySQL database (MariaDB) | 3306 | `mysql` | exact | authenticates as airship (credlist; fix_services creates the DB user) |
| db01 | System management SSH | 22 | `ssh` | substituted | local-credlist check (same domain-dimension limit as web01 SSH) |

## Credentials

- Note: Packet local-account password is n0t_sus1 (trailing one) while the domain password is n0t_sus! (bang) — encoded exactly as published. MySQL root/n0t_sus1 exists in the packet table; the engine's Sql check authenticates as credlist users, and fix_services grants credlist users full MySQL privileges.
- Box login `blueteam` uses the packet-published password (passwords.json, 0600 + gitignored) — teams change it at minute zero, exactly like the real event.
- Scoring credlist `linux.credlist`: `blueteam`, `airship`
- Scoring credlist `domain.credlist`: `blueteam`
- AD account `blueteam` (domain admin) planted per team via domain_accounts.json
- AD account `Red` (domain admin) planted per team via domain_accounts.json
- AD account `Blue` (domain admin) planted per team via domain_accounts.json
- AD account `Green` (domain admin) planted per team via domain_accounts.json
- AD account `Pink` (domain admin) planted per team via domain_accounts.json
- AD account `Orange` (domain admin) planted per team via domain_accounts.json
- AD account `Yellow` (domain admin) planted per team via domain_accounts.json
- AD account `Black` (domain admin) planted per team via domain_accounts.json
- AD account `White` (domain admin) planted per team via domain_accounts.json
- AD account `Purple` (domain admin) planted per team via domain_accounts.json
- AD account `Brown` (domain admin) planted per team via domain_accounts.json
- Out-of-scope decoy `scorebot` planted on every managed box with a random password (packet rule: these accounts exist, are never used for harm, and must not be touched)
- Out-of-scope decoy `blackteam` planted on every managed box with a random password (packet rule: these accounts exist, are never used for harm, and must not be touched)
- Out-of-scope decoy `red_scoring` planted on every managed box with a random password (packet rule: these accounts exist, are never used for harm, and must not be touched)

## Scoring model

- Packet weights: {'uptime': 40, 'injects': 30, 'red': 30}. The engine scores flat 5 pts/check/round — uptime weight is what the engine measures; injects are graded by white team over Quotient submissions; the red-team component is scored from bad-auto evidence, not the scoreboard.

## Schedule (offsets from T0)

| Window | Minute (T0+) | Note |
|---|---|---|
| check-in | — | 08:00 arrive (packet schedule) |
| system-tests | — | 09:00-09:30 — before T0; deploy finishes ahead of this |
| start | 0 | 09:30 competition active — engine unpauses (run-schedule.py start) |
| lunch-freeze | 150 | 12:00 lunch, hands-off keyboards — engine pause |
| lunch-resume | 195 | 12:45 competition active — engine unpause |
| ceo-meetings | 210 | 13:00-15:00 CEO meetings (injects still flow) |
| end | 390 | 16:00 scoring ends — engine pause + final scoreboard dump |

`run-schedule.py` executes the freeze/resume windows against the live engine; engine pausing is the only schedule primitive.

## Gaps

- none recorded (see substituted entries above for the honest deltas)
