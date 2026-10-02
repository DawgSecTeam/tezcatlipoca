# shakedown-5x4-2026-09-28 — full-stack readiness shakedown report

**Goal.** One competition exercising everything together: 5 boxes (Windows AD DC + Windows
member, Ubuntu, Fedora, Alpine), 4 teams, 2 scored services per box (AD counts — scored as
an LDAP Tcp check), ≥40 planted misconfigs per box, 3-hour automated red (bad-auto) vs
automated blue event, validating the whole tezcatlipoca pipeline end to end.

**Status at writing: the range deployed, verified green and frozen; the node (.193) went
hard-down during red01 bootstrap, before the event clock started. The 3h event is pending
node recovery (physical/IPMI). Everything below is what the shakedown actually proved and
fixed.**

## What was proven green

- **Deploy pipeline at 4×5 scale on .193** — first time: 20 team clones + engine + 5
  goldens ≈ 27 VMs. All seven phases completed across the repair cycles; datastore
  headroom gate, vmid-collision gate, template resolution, nakon bundle lint all did
  their jobs (three of them caught real problems before any node mutation).
- **`verify-competition.py` full green** on the final state: scoreboard + 4 team logins,
  credential hygiene, isolation rule *and* live cross-team probe (web01↔web01 blocked),
  misconfig spot-checks + survival, 5/5 injects, plant integrity (0 FAILED steps), all 4
  domains promoted with **unique DomainSIDs per team**, win01+web01+app01 domain-joined
  per team, **10/10 scored services UP on every team** — including the new AD (Tcp 389)
  and IIS (Web 80) checks and Alpine nginx+ssh.
- **AD as a scored service** (this comp's code change): `ADDS` pins map to a Quotient Tcp
  389 check and `IIS HTTP` to a Web check; `DOMAIN_INFRA_CONFIGS` keeps ADDS/Domain Join
  out of the nakon machine list (domain_ops owns them per-team at phase 6) while they
  stay scored. Both mappings exercised live across 4 teams.
- **226 pinned misconfigs across 5 boxes** (dc01 42, win01 40, web01 52, app01 49,
  edge01 43) — catalog check 0 errors; final sweep recorded **0 failed steps**. The Alpine
  leg — zero plants ever before — took 43 pins with the 9 systemd/cron/apt-family
  failures swapped for distro-agnostic families.
- **Automated blue harness staged**: fire test passed (service down detected → restored →
  healed), 4 blue workdirs ready on gpt-5.6-luna, `--blue-watchdog` armed.

## What the shakedown fixed (pipeline code)

- `quotient/setup.py`: ADDS → Tcp 389, IIS HTTP → Web 80 mappings.
- `nakon_ops`/`constants.py`: `DOMAIN_INFRA_CONFIGS` filter — scored-but-domain-owned
  configs never ride the nakon machine list.
- `hardening_ops.py`: `ALPINE_SERVICES["ssh"]`; alpine service shim prefers the
  **guest-agent root channel** (ssh+sudo dies once `writable-sudoers` 777s sudoers.d).
- `verify-competition.py`: isolation live-probe prefers a Linux box per team (the medic
  probe against a Windows DC always failed auth → isolation read as SKIP/FAIL).
- `build_alpine_ci_template.sh`: five fixes — quoted snippet heredoc + YAML-scalar
  quoting (an unquoted `NOPASSWD: ALL` parses as a mapping and cloud-init silently drops
  the whole runcmd), bootstrap verify gate (busybox `command -v` semantics; cloud-init
  "degraded" exit), package re-assert in final stage, ipv6 off (dl-cdn AAAA hang),
  persistent `ssh_pwauth: true` + `%wheel NOPASSWD` overrides, bootcmd agent start, and a
  pyyaml shape gate on the generated snippet.

## Findings (summary — full detail in [Findings detail](#findings-detail) below)

1. ifupdown2 stale `/run/network/ifstatenew` broke all bridge-create reloads (pfsense-era
   rot exposed by `ip link del` teardown); fixed on the node.
2. challenge-* templates sit on vmids 1210–1211 → default team ids collide on .193;
   run with `TF_VAR_team_identifiers=120,121,122,123`.
3. `writable-sudoers` 777s sudoers.d → password-sudo survives only via group rules;
   alpine needed the template wheel line, the shim needed the guest agent.
4. `.postclone-swept` marker isn't invalidated by phase-4 re-clones — fresh clones skip
   the sweep (TODO in deploy.py; workaround: clear the marker).
5. Engine VM DHCP drift + an unexplained full content-reversion after reboot; recovered
   via netplan pin + `--from-phase 3` + coverage replay. Engine is now pinned static.
6. AD-misconfig plants only run with first promotion — resume skips them; replayed
   manually per DC (svc-support etc. all planted on 4/4 DCs).
7. bad-auto's red01 bootstrap has no retry on transient pvestatd 596s; four launches
   each died on one 596. Follow-up: retry/await in bad-auto's agent execs.
8. Freezing is not commit-safe: committing after `--freeze` trips the drift gate.
9. **Node .193 went hard-down** mid-prep (unreachable from inside the lab as well).
   Root cause is a **known hardware issue of the cyberfield box** (owner-confirmed) — not
   the deploy load; the node stayed up through the whole load-28 evening and died
   afterwards. Needs a physical power cycle; everything deployed came back intact. The
   event was merely delayed by it (see Event Results).

## Readiness verdict

**Conditional GO.** The deploy/verify/plant machinery — the part this shakedown was
really testing — is proven at the target scale and is in the best shape it's been: the
gates caught three real definition bugs before touching the node, the repair loops all
converged, and the final state is fully green and frozen. What is NOT yet proven is the
3-hour red-vs-blue event itself: it never started because the node died during red01
bootstrap. When the node is back: `ping 10.0.0.193`, confirm `qm list` shows the 21-VM
range, then relaunch:

```
TF_VAR_team_identifiers="120,121,122,123" python3 -u run-agent-scrim.py \
  --competition shakedown-5x4-2026-09-28 --teams 4 --duration-min 180 --blue-watchdog \
  --skip-deploy --red-ip 10.0.0.244 --red-gw 10.0.0.1 --red-storage hdrives-zfs \
  --red-vmid 999 --red-template base-ubuntu24.04-fix
```

(re-run `verify-competition.py --engine-ip <fresh .233-or-newer ip from credentials.txt>
first — if the engine rebooted its IP can move; it is pinned static via netplan now.)

Red/blue notes for the event: bad-auto's dry-run already produced sane tactic choices
(cred_spray on real team IPs); intel mode is `nakon` so red knows the planted creds;
blues staggered per team with the watchdog for LLM outages. The engine's scoring round
loop may need `POST /api/competition/start {"started":true}` + `/api/engine/pause
{"pause":false}` if the node's return involved another engine reboot.

## Cost

Roughly 7 wall hours of which ~5 were diagnosis/repair of the four genuine pipeline bugs
above — exactly the class of thing this shakedown existed to catch before a real
competition. The alternative (finding the alpine template gaps and the sudoers/swep
interactions mid-event with 4 teams waiting) would have cost the event.

## EVENT RESULTS (2026-09-29 04:32–07:32 UTC, after node recovery)

The 3-hour event ran to completion after the node power cycle. All fixes from the
pre-event churn held; two more red-auto fixes landed during launch (596-retry in
`badauto/deploy/proxmox.py`, and intel files now ship to red01 via scp — the agent-exec
base64 channel 596s on multi-hundred-KB bodies). Harness fixes for 4-team runs: blue
staging/threads/evidence now scale with `--teams` (was hardcoded to 2), and each team's
isolated opencode HOME gets a unique service port (opencode's background service binds
one fixed port per instance — teams 2+ all died with port-in-use).

**Red (bad-auto)**: 129 ok / 16 failed actions; 21 takedowns (nginx ×16, httpd ×5 via
stop_mask + firewall blocks); 6 distinct tactics (cred_spray, foothold_ssh, privesc_linux,
beacon_plant, impact_service, impact_firewall); 2 initial-access techniques; **2 Windows
footholds**; touched every box. 12 red-observed blue restorations, 3 re-kill reactions.
Gate fails: max_simultaneous_down 1 (never got 2+ services down at once — the watchdog+
blues outpaced it) and 2 stalls (a 315s opening quiet period + one 13-min mid-event lull).

**Blue**: 39/42 cycles rc=0 (the 3 fails were team4's port collision before the orphan
kill); 10 restorations with 5–15 min time-to-restore; **2 evictions** (footholds removed);
92 notebook entries; 0 timeouts; down-minutes ≈ 0 on teams 1/2. Final: team1 10/10 UP,
team2 10/10 UP (after absorbing 8 of red's takedowns), team3 9/10 (one late web01-http
kill at T+170), team4 10/10 UP. A notable real dynamic: team2's web01 stopped accepting
password SSH at ~T+125 — the blue closed the entry vector after repeated restores
(exactly the behavior the cycle prompt prescribes).

**Gates: 9/12 PASS.** The 3 fails: red max_simultaneous_down (blue won availability),
red stalls (opening quiet period — the known bad-auto "too quiet" issue), and blue
injects 0 — the injects' clocks were anchored at the original phase-7 seed hours before
T0, so all 5 were already expired at T0 (`--skip-deploy` restarts don't re-anchor
injects; use `redeploy --reset-event`, or re-anchor injects at T0 — harness TODO).

**bad-auto red-auto follow-ups**: opening stall (first decision ~5 min late), escalate
from single-target nginx loops toward simultaneous multi-service impact when restorations
outpace re-kills, fix phantom `db01` target name in the intel model.

**Harness follow-ups**: re-anchor injects at T0; monitor scoreboard capture + final
services evidence still loop over 2 teams; end-of-event teams-only teardown destroys the
engine (and its scoring DB) *before* the report can read the final scores — capture the
final scoreboard dump into the evidence dir first.

*All of the above landed 2026-09-29* — bad-auto 642675c (90 s first-decision budget;
restore-aware reimpact cooldown + same-cycle press bursts up to `press_impacts_per_cycle`;
phantom box names flagged in report summaries) and tezcatlipoca ce491e9/8a83b05 (T0 inject
re-anchor via UpdateInject; every scoreboard/teardown/report loop scaled past 2 teams;
`evidence/final-scoreboard.json` dumped before teardown; details in
[known-issues.md](../known-issues.md) § shakedown-5x4 event).

## Findings detail

- **ifupdown2 stale `/run/network/ifstatenew` breaks every network reload** (2026-09-28).
  After team bridges are removed with `ip link del` (instead of `ifdown`), ifupdown2's
  pickled state still lists the deleted bridges; every later `ifreload -a` (including the
  one the terraform proxmox provider runs per bridge create) dies with
  `[Errno 2] ... /sys/class/net/vmbrNNN/brif/` — deploy phase 2 fails on all four bridge
  creates. Fix on the node: `rm /run/network/ifstatenew && ifreload -a`. Rule: tear bridges
  down with `ifdown` (state-updating), not raw `ip link del`. The node's ifstatenew dated
  from the Sep 27 pfsense churn — the rot predated us; our `ip link del` merely exposed it.

- **challenge-* templates occupy vmids 1210–1211** — default team identifiers 101–104
  collide on .193 (team1 = 1210–1214). The vmid-collision preflight gate catches it and
  tells you exactly which vmids; always pass `TF_VAR_team_identifiers` (120–123 →
  1400–1434 is the proven choice) when the node hosts challenge templates.

- **`build_alpine_ci_template.sh` needed five fixes** (shakedown-5x4): (1) the cloud-init
  snippet heredoc was unquoted (`<<YAML`), so backticks in a *comment* ran as command
  substitution; (2) the bootstrap verify used busybox multi-arg `command -v`, which exits 2
  even when everything is installed — the old run "passed" only because nothing read the
  exit code; the gate now checks binaries one `command -v` per item and skips
  `cloud-init status`'s own nonzero exit on "degraded"; (3) cloud-init's `packages:` races
  first-boot network — the full set is re-asserted via `apk add` in runcmd; (4) an unquoted
  YAML scalar containing `NOPASSWD: ALL` parses as a **mapping** (colon-space) and
  cloud-init silently drops the whole runcmd — scalars are quoted now and the snippet
  shape is gated through pyyaml at build time; (5) dl-cdn resolves AAAA and the lab has no
  v6 egress — apk hangs on v6 connects (the "random missing packages per boot" flake);
  bootcmd disables ipv6 (persists to clones, which also fixes the shim's apk).

- **Alpine clones need PERSISTENT `ssh_pwauth` + a `%wheel` sudoers rule in the MAIN
  sudoers file.** The image ships `PasswordAuthentication no` and `ssh_pwauth: false`;
  a clone's first-boot cloud-init re-disables password auth even if the template fixed
  sshd_config, so the golden plant dies with "Bad authentication type". And alpine's sudo
  ships with no `%wheel` rule — the box user's NOPASSWD lives only in sudoers.d, so the
  moment a plant like `writable-sudoers` makes sudoers.d world-writable, sudo ignores the
  entire dir and the pipeline (shim, fix_services, beacons, nakon) loses root. Both are
  baked into the -fix template now (`99-tz-pwauth.cfg`, main-sudoers wheel line).

- **`writable-sudoers` plants sudoers.d as 0777 → sudo refuses the whole dir.** ubuntu/
  fedora survive because medic is in the `sudo`/`wheel` group and nakon authenticates with
  `sudo -S` (password via stdin); alpine needed the template fix above. The **alpine
  service shim now prefers the guest-agent root channel** (ssh+sudo is the fallback);
  `fix_services_on_boxes` already had the same fallback. Anything new that sh-commands
  `sudo` on a post-sweep box must assume password-sudo at best.

- **The `.postclone-swept` marker was not invalidated when a resume re-cloned team boxes**
  (phase 4 re-entry) — FIXED 2026-09-29 (8a83b05): deploy unlinks the marker right after
  apply #2 succeeds, so a following phase-5 always re-sweeps the fresh clones. Hit twice on
  shakedown-5x4; the old workaround (`rm competitions/<id>/.postclone-swept` before any
  `--from-phase 4|5` resume that re-created boxes) is no longer needed.

- **Engine VM DHCP drift + full-content reversion.** The engine ran `ipconfig0 ip=dhcp`;
  an unexplained reboot (during a heavily-loaded churn window) brought it back on a
  different IP (.221→.243→.233) while terraform's saved output — which deploy/verify/
  credentials all consume — stayed stale, and the fresh boot had *lost* the whole
  post-clone system state (no docker, no /opt/quotient, no paramiko; only /opt/nakon from
  the sweeps). Recovery that worked: pin the guest static via netplan +
  `cloud-init network: {config: disabled}` (state output patched to match), then
  `--from-phase 3` to re-prep the engine, `--from-phase 5` after clearing the swept marker
  (clones were re-cloned by the same churn), and re-run the AD-misconfig pass (it only
  runs with first promotion — replayed via
  `domain_ops._run_single_nakon_config` with the 4 AD configs per DC). Both follow-ups
  landed 2026-09-29 (8a83b05): the engine gets a STATIC mgmt IP by default
  (`10.0.0.250`, `TF_VAR_engine_mgmt_ip` override, `''` = DHCP, preflight refuses a
  colliding address), and phase 6 replays the AD plants whenever the
  `.nakon-domain-<team>-ad-misconfigs` marker is missing, even when the domain is up.

- **Freeze gate vs commits:** the frozen record includes the code tree state; committing
  after `--freeze` trips the drift gate on the next deploy ("golden template changed
  since FREEZE: golden_configs"). Freeze LAST, after the final commit; if you must,
  `--unfreeze --confirm-unfreeze` (pre-competition only) and re-freeze.

- **bad-auto red01 bootstrap vs transient pvestatd 596s — FIXED.** The node threw
  HTTP 596 (broken pipe) on `qemu/999/agent/exec` bursts under load ~28, killing bad-auto's
  deploy mid-bootstrap four times; each relaunch got a bit further (idempotent), but a
  wedged apt inside red01 (stale lock from a killed attempt) also needed
  `pkill -9 apt-get; dpkg --configure -a` through the guest agent. Both follow-ups landed:
  agent execs retry through 596/5xx bursts (bad-auto a542df7), and the apt install loop now
  clears a stale dpkg/apt lock (`pkill -9 apt-get` + `dpkg --configure -a`) before each
  retry instead of burning all six attempts on "Could not get lock" (bad-auto 642675c).

- **Node .193 hard-downs are a KNOWN HARDWARE ISSUE of the cyberfield box** (owner
  confirmed 2026-09-29 — not load-related; the shakedown evening hit load 28 with ~27 VMs
  and the node stayed up through all of it). Failure mode observed 2026-09-28: complete
  loss of ping/SSH/API (even .150 on the same lab segment cannot see it) with no
  self-recovery for 1.5+ h; a manual power cycle brought it back, and every running VM
  (the full 27-VM range) came back intact. Plan around it: anything deployed there can
  vanish with the node at any time; recovery = physical power cycle + re-verify (the
  scoring loop needs `POST /api/competition/start` + `/api/engine/pause` after the
  engine reboots).

## Event-gate findings detail (moved from known-issues.md)

The first 4-team event (gates 9/12, [report](shakedown-5x4-2026-09-28-report.md)) failed three
gates for reasons that were all fixable driver/harness defects, not scenario problems:

1. **Blue injects = 0 — inject clocks anchored at phase-7 deploy time.** `resolve_inject_times`
   anchors offsets at phase 7; a `--skip-deploy` restart (or a long staging gap) reaches T0 with
   every inject already expired. This is the same disease as the winad-scrim2 closed-injects
   incident, on the scrim path. FIXED (tezcatlipoca ce491e9): `run-agent-scrim` re-anchors at T0
   unconditionally — `POST /api/injects/{id}` (the engine's UpdateInject; keep-files re-lists
   attachments because unlisted files get deleted) with times recomputed from the comp's
   `injects/*/inject.json` offsets, matched by title. Unordered offsets fail before the event.
   `redeploy --reset-event` (engine re-clone) remains the heavy fallback, no longer required.
2. **Red max_simultaneous_down = 1 + a 315s opening quiet period.** Two bad-auto defects
   (fixed in bad-auto 642675c): (a) the FIRST decision could legally run the full 360 s LLM
   budget before the first attack action — the first decision now gets a 90 s budget
   (`first_decision_budget_sec`) and the deterministic fallback acts on expiry; (b) the loop is
   one-action-per-cycle with a flat 15-min `reimpact_after_min` exclusivity per unit, so blue's
   restorations outpaced re-kills — a detected restore now cancels that unit's exclusivity
   immediately, and while press tempo is active red chains up to `press_impacts_per_cycle` (3)
   additional DISTINCT unit kills in the same cycle (still through validate_decision, with
   per-team headroom tracked between the scoreboard's ~70 s polls).
3. **Teams 3/4 invisible to the evidence loops.** `monitor_loop`, `stage_capture`, the blue
   endpoint/lock selection, `scrim-report`'s `host_label` + `blue_metrics`, and the final-scores
   read all assumed 2 teams — 4-team runs got scoreboard jsonl, final-services JSON, and gate
   counts for teams 1/2 only, and teams 3/4 silently shared team1's LLM endpoint and lock.
   FIXED (ce491e9): everything scales with `--teams` (`_cred_team_names`; per-team
   `--blue{N}-base-url/--blue{N}-model`, one lock per distinct endpoint), and identifiers
   100+i map to team i generically.

Also fixed that night: **teardown destroyed the engine (and its scoring DB) before the report
could read final scores** — `stage_capture` now dumps `evidence/final-scoreboard.json` (teams,
injects, per-team services) BEFORE teardown, and `scrim-report` renders a "Final scores" section
from it; and **bad-auto's report could name boxes that never existed** (a `db01` in the
shakedown report for a range of dc01/win01/web01/app01/edge01) — LLM executive summaries get
phantom box names flagged in place (bad-auto 642675c).
