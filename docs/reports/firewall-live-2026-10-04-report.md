# Firewall live validation — fw-live-2026-10-04 (2026-10-04)

Stage A of [the live-validation plan](firewall-live-validation-plan-2026-10-04.md), executed
to completion on cyberrange `.150`: one team (`141`) — `fw01` (template `956 pfsense`, in-path)
+ `web01` (nginx, scored) + `db01` (unpinned) — deployed through phase 8, verified, drilled,
torn down. **The pipeline-v3 in-path firewall is live-proven end to end.** Stage B
(pfsense-rvb, Windows DC) was NOT run — the parallel session's three concurrent deploys on
`.150` made the extra footprint unreasonable, and the Windows-through-firewall path remains
the one unproven leg (see Open items).

## What was proven

- Terraform builds the new shape: `vmbrW141` transit bridge (apply #1), engine transit NIC
  (`ens20` at `172.31.141.1/30`), two-NIC firewall clone (WAN transit / LAN team bridge),
  no cloud-init on the firewall; a firewallless comp's plan-only output is unchanged.
- Phase 5 live: config generated from the seed and served from the engine (`:8611`), console
  drive via QEMU-monitor `sendkey`, the pfSense fetched `config-team141.xml` (HTTP 200 in the
  engine's access log), rebooted into it, and answered SSH on its WAN. Cutover moved
  `192.168.141.1` off the engine; `ip route get` shows the team subnet **via 172.31.141.2**;
  web01:22 reachable through the firewall; `verify-competition.py`: logins/services/pins/
  isolation/plant-coverage/round-loop all **PASS**, zero degradations. (The `misconfig` FAIL
  is by construction — this comp ships no misconfig pins.)
- The drill: `--from-phase 5` on the LIVE range re-drove the console (green on drive #1),
  re-applied the cutover, and re-verified — the documented remedy for the terraform-reapply
  netplan caveat works as advertised.
- Teardown: teams-only destroy reclaimed **both** `vmbr141` and `vmbrW141`; goldens +
  engine-template kept per M4. Artifacts archived to
  `~/.tezcatlipoca/automated-tests/fw-live-2026-10-04/`.

## Live-found fixes (all in this branch, merged to main)

1. **Zero-step plans hard-fail strict passes** (`a628b59`) — an unpinned box dispatches an
   empty plan; nakon's runner misreads the silent no-op as "no output from the remote plan
   (exit 0) — check credentials" and fails the STRICT golden plant (reproduced twice on
   fresh clones; the box itself accepted SSH+sudo throughout). Driver-side reconciliation
   (`nakon_ops._reconcile_zero_step_machines`); upstream brief added as
   upstream-defects-handoff **#11**.
2. **The firewall's LAN NIC was never emitted** — the `in_path` terraform branch emitted
   only the WAN device, so the firewall booted with no LAN and the bootstrap could never
   fetch (the plan showed a single `network_device`). Fixed to emit `vmbrW<id>` + `vmbr<id>`;
   firewalls also carry `disk_gb: null` now (the template's own IDE disk is the boot disk —
   the extra 12G scsi disk was dead weight).
3. **The phase-5 success probe was unroutable by design** — the generated config's only WAN
   rule permitted any→lan (forwarded traffic), but the probe SSHes the firewall ITSELF;
   pfSense has no implicit management allowance on WAN. The config now also passes tcp/22 to
   `172.31.<id>.2`. (Two console fetches of a *correct* config failed the probe for ~2h
   before this was found — the fetch lines in the engine's http.log were the tell.)
4. **Orphaned `:8611` listeners starve the bootstrap** — a driver killed mid-phase-5 leaves
   its config HTTP server holding the port (the `finally` never runs); every later console
   drive then fetches into the void. The bootstrap now clears pre-existing listeners and
   PROVES the bind before driving.
5. **`write_team_configs` had an unused required param** (`fw_box`) — the phase call
   TypeError'd on first phase-5 entry.
6. Diagnostics: `console_screenshot`'s RFB flow documented (PVE's 3.8 type-list path does
   NOT complete over `vncwebsocket`; offer `RFB 003.003` and it behaves 3.3-style), and the
   websocket is now closed on every path (a leaked session breaks the next screenshot).

## Operational notes for the next firewall run

- Two deploys raced this run's identifiers (a foreign `engine-template` squatted the first
  chosen vmid block; three concurrent deploys shared the node). The preflight collision
  gate caught all of it — pick identifiers by scanning at launch time, not before.
- Guest reboots are INVISIBLE in `qm status` uptime (that counter is the QEMU process) —
  do not use it to judge whether the console `reboot` fired; use the engine's http.log and
  the WAN probe.
- pfSense console screenshots work (fixed flow) but are timing-fragile under concurrent
  node load; the functional probe is the primary signal.

## Open items

- **Stage B not run**: Windows DC + domain join through the firewall is unproven (the
  runbook proved it manually in the pfsense-ad era; the pipeline path differs only in
  who drove the config). Any firewall comp with a DC should watch the ADDS/join phase
  specifically.
- The `misconfig` verify gate fails any comp with no misconfig pins — arguably an
  informational SKIP for deliberately-clean infrastructure comps; left as-is (a comp
  without misconfigs is not a shippable competition anyway).
- Screenshot timing fragility under node load (see operational notes).
