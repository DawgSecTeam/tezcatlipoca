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

## Findings (full detail in docs/known-issues.md)

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
[known-issues.md](known-issues.md) § shakedown-5x4 event).
