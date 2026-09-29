# svc-matrix-2026-09-28 run report

Purpose-built matrix competition: **every** supported scored service — every entry in
`quotient/setup.py:_SERVICE_TO_CHECK` — on Windows and Linux, two teams, zero planted
vulns. It doubled as the live validation target for bad-auto's takedown coverage
(sibling repo, branch `svc-matrix-takedowns`): every scored service must be takeable
down and restorable. Both goals were met on 2026-09-28.

## Lineup

Node `proxmox` (cyberrange, 10.0.0.150), datastore **wkshp-pool** (hdd/ssd fail the
~470 GB preflight gate — see [known-issues.md](known-issues.md)), scoring vmid 1050,
engine at 10.0.0.190, teams ident 130/131 → vmids 1500–1519, goldens 1200–1209,
engine template 1190.

| box | template | pins → scoreboard name |
|---|---|---|
| dc01 | base-windows-server | ADDS → `dc01-ldap` (:389, domain `team<id>.local` via domain_roles "dc") |
| win01 | base-windows-server | Enable WinRM, New SMB Share, RDP misconfigs, IIS HTTP |
| web01–web04 | base-ubuntu24.04-fix | apache, nginx, splunk (:8000), roundcube — one per box |
| db01 | base-ubuntu24.04-fix | mysql |
| dns01 | base-ubuntu24.04-fix | bind |
| mail01 | base-ubuntu24.04-fix | postfix, dovecot |
| ssh01 | base-debian13-lite-fix | ssh, telnet-service, vsftpd |

`box_vulns.json` is all-empty by design: services are pinned, nothing randomizes, and
verify runs with `--expect-no-vulns` (the misconfig gates would otherwise FAIL on a
deliberately clean range).

## Result

- Deploy: 7/7 phases, one mid-flight fix (golden-web03 disk, see known-issues).
- `verify-competition.py --strict-services`: **16/16 services UP on both teams**;
  logins, no-default-creds, plant coverage, domains (unique DomainSIDs per team) PASS.
- bad-auto takedown coverage: **16/16 taken down and restored** from red01 over the
  engine-NAT path, e.g. ADDS 90s down / 30s up, WinRM·SMB·RDP·IIS ~90s/30s,
  splunk & roundcube 60s/60s, ssh (credlist sabotage) 90s/30s with the restored login
  auth-verified. Tooling: `python3 -m badauto coverage` (bad-auto `svc-matrix-takedowns`).

## What the run changed in the code

- tezcatlipoca (`svc-matrix-2026-09-28`): bind duplicate-zone fix, lighttpd
  restart-not-start, RDP firewall rules in `bootstrap_windows_box`
  (`golden_ops.ensure_golden_disk_size` + tests; `verify-competition.py
  --expect-no-vulns`) — details in [known-issues.md](known-issues.md).
- bad-auto (`svc-matrix-takedowns`): `coverage` subcommand; powershell double-quote
  exec wrapping; ADDS/SMB/RDP effect rework; credlist restore from intel ground
  truth. Invariants in that repo's AGENTS.md.

## Teardown

`python3 destroy-competition.py --competition svc-matrix-2026-09-28 --full --yes`
(from the worktree; the range was never frozen, so `--end-of-competition` is not
required). Confirms 1050/1190/1200–1209/1500–1519 and vmbr130/131 gone from node
proxmox.
