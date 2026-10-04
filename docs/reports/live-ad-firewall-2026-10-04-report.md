# Live AD + in-path firewall test, pfsense-ad, 1 team (2026-10-04) - PARTIAL

Worktree ../tezcatlipoca-live-ad, branch live-ad-2026-10-04, node .150. Deviations from the brief:
scoring vmid 1890 (1900 collides with team vmid 200+170*10 = 1900), engine mgmt IP 10.0.0.243
(.247 is answered by another session's engine 1060).

## Stages
- Phases 1-4: PASS. Engine template, golden set (Windows DC unbooted golden, ubuntu, fedora), team clones, tz-base snapshots.
- Phase 5 (firewall bootstrap): FAIL, two pre-existing v3 bugs (not refactor regressions):
  1. write_team_configs() required an unused `fw_box` -> TypeError. Fixed by cherry-pick 004f5b4 (from offline-packets b2f832d).
  2. terraform gave the in-path firewall only net0 (WAN, vmbrW170); the LAN NIC was suppressed by `in_path ? [] : [bridge]`.
     Console drive therefore failed 3x ("not up yet"), phase aborted with the console-screenshot error. Fixed in terraform/main.tf
     with offline test tests/test_terraform_firewall_nics.py (commit on this branch).
- Phases 6-8, verify, redeploy modes, destroy --full: NOT RUN. Applying the fix to the live range (add net1 to VM 1903, via API or
  `--from-phase 4` terraform re-apply) was denied by the permission classifier (shared-resource modification / blind apply). Needs operator approval.

## Other findings
- console_screenshot() VNC websocket failed ("Connection to remote host was lost"), so the failure PNG is unavailable (diagnostic only; unrooted).
- Offline test tests/test_multinode_offline_e2e.py fails in the full suite when .env sets TF_VAR_engine_mgmt_ip (env leak; passes alone and in a clean env).
- Phase 1 ~10 min, phase 2-4 ~40 min total (serial image pulls and Windows bootstrap).
- Preflight engine-mgmt-IP check works (it refused .247).

## Left running
Range pfsense-ad (engine 1890, goldens 2040+, team1 boxes 1900-1903, bridges vmbr170/vmbrW170) is still up pending approval to continue; teardown is
`python3 destroy-competition.py --competition pfsense-ad --full --yes` from the worktree.
