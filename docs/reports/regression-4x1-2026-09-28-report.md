# regression-4x1-2026-09-28 — minimal validation comp for the changes-shake pass

One team × four boxes (dc01/win01 base-windows-server sata0 60G, web01 ubuntu24.04-fix 30G,
ssh01 debian13-lite-fix 15G), 12 scored pins, no planted vulns, engine vmid 1090, team ident
125. Config in `competitions/regression-4x1-2026-09-28/` (committed); range torn down
`--full` at close (inventory clean, shakedown + pfsense comps untouched).

## What it validated (deploy + verify)

- `ensure_golden_disk_size` fired before first boot: `golden 1242: scsi0 grown 15G -> 30G`,
  `golden 1243: scsi0 grown 10G -> 15G`; Windows goldens (sata0) exercised the root-disk
  detection branch at their template size (grow-only no-op).
- **Live-found defect + fix:** the hypervisor-side resize never reaches an LVM root fs —
  cloud-init only grows plain partition+fs, so ubuntu's 10G root LV filled and bind's plant
  died `ENOSPC` (first deploy attempt). Fixed in `golden_ops.expand_guest_root_disks`
  (growpart + pvresize + lvextend + resize2fs post-boot, before the tz-base snapshot);
  proven live on re-entry: root LV 10G → 28.2G, full re-plant zero failures.
- `bootstrap_windows_box` with the firewall-profiles enable + RDP rule enables; phase-4b
  bootstraps green on both Windows clones (no 90s agent wedge).
- Phase-5 `fix_services_on_boxes` (bind duplicate-zone strip, lighttpd restart) green —
  splunk :8000 and Dns checks UP.
- `DOMAIN_INFRA_CONFIGS` stripping: generated `nakon-config.json` contains 0 ADDS/domain-join
  entries; ADDS still scored as `dc01-ldap` (Tcp :389).
- `verify-competition.py --strict-services --expect-no-vulns`: **RESULT PASS** — logins,
  no_default_creds, services 12/12 UP (strict), isolation, domains (team125.local
  DomainSID), plant coverage PASS; misconfig rows render `SKIP (--expect-no-vulns)`.
  Re-run PASS after the node outage with byte-identical template hashes.

## bad-auto takedown coverage (the changed effect layer)

- Run 1 (red01 = vmid 999 reconfigured, `.244`): 8/12 — ADDS (profile-enable + `:389` block
  rule flipping a scored check DOWN, proving the DC firewall-profiles fix end-to-end),
  WinRM, SMB, RDP, IIS (Windows postchecks), apache, splunk (lighttpd alias), bind.
- Run 2 (own red01 = vmid 998, `.245`): 3/3 — ssh (credlist_sabotage + restore-password-
  from-intel + SSH-proven re-auth), telnet-service (inetd alias), vsftpd.
- **11/12 pins proven down+restored live.** The 12th (roundcube) is impossible in this comp
  shape: two same-check-TYPE pins on one box collapse to one scoreboard check (see
  known-issues); the alias mechanism itself is proven by the splunk/telnet rows.
  Evidence: `coverage-run1.json`, `coverage-ssh01.json` in the comp dir.

## Operational notes

- Node .193 went hard-down mid-pass (~00:05 2026-09-29, load-28 evening across two
  concurrent deploys); recovered ~06:15. Post-recovery: engine VM auto-rebooted →
  scoreboard needed the documented round-loop resume (`POST /api/competition/start
  {"started":true}` + `/api/engine/pause {"pause":false}`) and the team boxes needed
  starting; everything else survived.
- Cross-session collision: the parallel shakedown session committed to main
  (`f5a6967`) while the shared tree had this branch checked out, and rewrote bad-auto's
  `config.yaml` twice (red VM 999/.244 is theirs). My red01 was redeployed on its own
  vmid 998/`.245` to avoid contention; teardown was done manually (beacon STOP, NAT rule
  removal on the engine, VM delete) instead of `badauto destroy`, which reads
  `config.yaml` and would have targeted THEIR red01.
