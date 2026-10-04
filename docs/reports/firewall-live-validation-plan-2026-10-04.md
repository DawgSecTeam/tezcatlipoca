# Firewall integration — live validation plan (2026-10-04)

Pipeline v3 (e652681) shipped the in-path firewall support **code-complete but never
live-proven**. This plan validates it end to end on cyberrange `.150` (the only node with
the pfSense template, `956 pfsense`), in two stages of increasing realism, plus failure
drills and teardown. Everything runs from a NEW worktree per AGENTS.md — the main tree has
an active parallel session and practice deploys are exactly the runs that crash.

## What must be proven

1. Terraform builds the new shape for real: `vmbrW<id>` transit bridges, engine transit
   NICs, two-NIC firewall clones (WAN transit / LAN team bridge), no cloud-init on the
   firewall — and that a firewallless lineup still plans byte-identical (no diff).
2. Phase 5 works live: per-team config generation from the seed, console fetch over the
   engine's HTTP server, WAN-SSH success probe, engine cutover, and the in-path routing
   gate — the blind console bootstrap is the single riskiest piece of the feature.
3. Everything downstream composes: nakon plants, scored services, verify gates — all
   through the firewall instead of the engine's gateway address.
4. Failure + recovery story: phase-5 resume re-drives, teardown reclaims `vmbrW<id>`.
5. Zero-regression for existing comps (firewallless): the stage-A plan-only diff check.

## Preconditions — run every check at execution time

- [ ] **Worktree**: `git fetch origin && git worktree add -b fw-live-2026-10-04
      ../tezcatlipoca-fw-live origin/main && cd ../tezcatlipoca-fw-live` — cut from the
      CURRENT tip (main moved twice while this was being written; per-box-reset work is
      landing). `git submodule update --init`; copy `.env`, `vendor/nakon/.env`,
      and the `proxmox` SSH key (`cp /path/to/main-tree/proxmox . && chmod 600 proxmox`).
- [ ] **Env variant**: base on `.env.realm-backup-20260923` (the `.150` variant) and FIX
      the documented stale vars before trusting it: `TF_VAR_template_vm_id=955` (the
      shipped file carries the dead 9106 — a wrong value here hard-fails the engine-base
      preflight), `TF_VAR_team_identifiers=140` (100–124 are occupied on .150; 130/131
      were svc-matrix's), `TEZ_THIN_HEADROOM=0.25` (cyberrange `hdd` is ZFS thin).
- [ ] **Estate scan on .150** (Proxmox API): template `956 pfsense` exists, tagged
      `template`, status stopped; chosen engine vmid free (use e.g. 1085 — then engine
      template lands at 1085+140=1225, goldens 1225+150…; adjust after scanning);
      team vmids 1600–1603 free (200+140·10+idx); bridges `vmbr140` AND `vmbrW140` free;
      datastore headroom (thin factor applies); no held flock in `~/.tezcatlipoca/locks/`;
      `pgrep -af 'create-competition|redeploy-competition'` empty — if the parallel
      session is mid-deploy, coordinate identifiers or wait; never set
      `TEZ_ALLOW_CONCURRENT` for this run.
- [ ] **No stale .150 firewall state**: check no leftover `vmbrW*` from the manual runbook
      era conflicts with the chosen team identifier.

## Stage A — minimal firewall comp (the fast path to phase 5)

One team, two Linux boxes, one firewall: the smallest lineup that exercises every new
code path (transit wiring, console bootstrap, cutover, routed plant/scoring).

In the worktree, author `competitions/fw-live-2026-10-04/` by hand (non-interactive, per
docs/usage-agents.md):

```
Compfile:      name fw-live-2026-10-04 / scenario <one line> / difficulty 3
users.json:    {"box_username": "ubuntu", "credlist_usernames": ["admin","user1","user2"]}
boxes.json:
  [{"name":"fw01","last_octet":1,"cpu":1,"memory_mb":512,"disk_gb":12,
    "template":"pfsense","unmanaged":true,"in_path":true},
   {"name":"web01","last_octet":2,"cpu":1,"memory_mb":1024,"template":"base-ubuntu24.04-fix"},
   {"name":"db01","last_octet":3,"cpu":1,"memory_mb":1024,"template":"base-debian13-lite-fix"}]
box_services.json / box_vulns.json: {"web01":[],"db01":[],"fw01":[]}  (created empty; add
  one service pin on web01 — e.g. the nginx apache-style pin from an existing comp — so
  the scored-through-firewall claim is actually scored, not just reachable)
```

Also copy the pfSense seed: `mkdir -p competitions/fw-live-2026-10-04/pfsense &&
cp competitions/pfsense-ad/pfsense/pfsense-config-orig.xml competitions/fw-live-2026-10-04/pfsense/`
(pfsense-ad's is git-tracked, so it exists in the worktree).

Run, all from the worktree root:

```bash
python3 create-competition.py --competition fw-live-2026-10-04 --plan-only   # fw01 line + "(in-path firewall …)" annotation
# zero-diff regression: same command against a firewallless comp (e.g. same-type-2box)
python3 create-competition.py --competition same-type-2box --plan-only
# real deploy — DETACHED (usage-agents.md → Running a deploy that outlives your shell):
setsid nohup python3 create-competition.py --competition fw-live-2026-10-04 \
    --teams 1 --scoring-vmid <free> --yes > logs/fw-live-stageA.log 2>&1 &
```

Watch points (progress reads from the log; `logs/` is gitignored):

| Phase | Expect |
|---|---|
| 1–2 | preflight names the firewall template as resolving; apply #1 plan includes `proxmox_network_linux_bridge.transit_bridge["team1"]` |
| 4 | apply #2 plan shows `fw01-team1` with TWO network devices; cloud-init block absent for fw01; `wait_boxes_ssh` covers web01/db01 only (no 300 s firewall hang, no new degradation entry) |
| 5 | `firewall_configs x1` → console drive lines → "firewall up — SSH answering on 172.31.140.2" → engine cutover → "In-path verified" → firewall `tz-base` snapshot. If it fails: `logs/fw-console-fw-live-2026-10-04-140.png` + the re-drive hint |
| 6–8 | repair/final nakon passes now route through the firewall (watch for any new plant failures vs a normal run) |

Post-deploy, prove the in-path claim from the engine (`ssh -i proxmox sysadmin@<engine>`):

```bash
ip route get 192.168.140.1        # must show via 172.31.140.2
ip -4 addr show | grep 172.31     # transit /30 up
ssh -o ... root@172.31.140.2 pfctl -sr   # WAN pass rule loaded (pfSense SSH is enabled by the generated config)
```

Then `python3 verify-competition.py competitions/fw-live-2026-10-04` — expect all gates
PASS (services gate IS the scored-through-firewall proof; isolation's engine-side FORWARD
read stays meaningful because team→team still transits the engine).

## Stage B — pfsense-rvb, one team (realistic composition)

`competitions/pfsense-rvb` is fully git-tracked (fw01 at `.1` in_path, seed, users.json)
and exercises the compose-with-everything case: Windows DC (unbooted golden + ADDS
promotion) + domain join + scored AD/Linux services with the firewall in path.

```bash
setsid nohup python3 create-competition.py --competition pfsense-rvb \
    --teams 1 --scoring-vmid <free> --yes > logs/fw-live-stageB.log 2>&1 &
```

Expected frictions (not bugs):
- The engine template REBUILDS even if pfsense-rvb deployed before: the M4 hash inputs
  include `main.tf`, which changed in v3. Budget the ~8–10 min template build.
- Domain join traffic now crosses the firewall: if ADDS/join fails, check the WAN pass
  rule and `pfctl -sr` BEFORE suspecting domain_ops — the runbook proved these paths
  work routed, but Domain Join was one of the reset live-fixes on main, so a failure
  here is also that fix's first firewall-path outing.
- `verify --packet` fidelity: pfsense-rvb's Compfile says fw01 is "left clean/undamaged"
  — verify's plant-coverage gate should record the fw as intentionally bare, not a gap.

## Failure drills (run after Stage A is green, same worktree + range)

1. **Phase-5 idempotent re-drive** — `python3 create-competition.py --competition
   fw-live-2026-10-04 --from-phase 5 --yes` on the LIVE range: consoles re-driven, cutover
   re-applied, verify passes again. This is also the documented remedy for the
   terraform-reapply netplan caveat, so it must work on a live range.
2. **Crash mid-phase-5** — kill the driver by PID during the phase-5 wait (kill the
   PYTHON driver, never `pkill -f` a pattern; pgrep first), then resume with
   `--from-phase 5 --yes`: the half-configured firewall is re-driven (fetch is
   idempotent), state flags/marker story holds.
3. **Teardown** — `python3 destroy-competition.py --competition fw-live-2026-10-04 --yes`
   (teams-only), then re-run until exit-clean; confirm via the API that `vmbr140` AND
   `vmbrW140` are gone, team vmids + engine free, and the teardown's artifact collection
   landed under `competitions/<id>/.automated-tests/<run-id>/`. Then
   `python3 test-artifacts.py archive fw-live-2026-10-04 --all` BEFORE discarding the
   worktree (per-comp dir is worktree-local).

## Abort / rollback

Any phase hard-failing twice (the resume-streak guard stops a third): kill by pid, run
the teardown, and record the failure + `logs/fw-console-*.png` in the report before
touching anything else. If phase 5 wedges a firewall mid-config, the fallback is the
manual runbook (docs/pfsense-inpath-2026-09-28.md — kept as the trap list) on the LIVE
clone, then `--from-phase 5` to re-drive the rest.

## Risks

| Risk | Mitigation |
|---|---|
| Blind console typing lands in the FreeBSD loader or the fetch never fires | `CONSOLE_SETTLE_S=90`, re-drives, functional WAN-SSH probe, failure PNG; worst case the manual runbook on the live clone |
| pfSense template drift (956 not tagged/stopped, or a stale clone squatting) | estate precheck; preflight's template gate names it |
| Parallel session deploys concurrently on .150 | pgrep + flock precheck; distinct team identifier 140 + scoring vmid; never TEZ_ALLOW_CONCURRENT |
| Stage B ADDS-through-firewall failure conflates two causes | Stage A is green first (routing proven with Linux boxes); then a domain failure points at the join path, not the firewall |
| Engine template rebuild cost on Stage B | expected (main.tf hash churn); budget it |

## Success criteria

Stage A: deploy reaches "[8/8] live" with every phase green; `ip route get` via the
transit; verify all-PASS; teardown clean including both bridges. Stage B: same, with
domain promotion + scored AD services through the firewall. Drills: resume-from-5 works
on the live range; destroy reclaims `vmbrW140`. Anything less lands in
docs/known-issues.md with the console PNG attached to the report.
