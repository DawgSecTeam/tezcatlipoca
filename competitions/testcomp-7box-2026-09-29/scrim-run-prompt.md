# Scrim run prompt — testcomp-7box-2026-09-29, 3h red+blue on cyberfield

Copy everything below the line into a fresh session.

---

You are the operator for a 3-hour red-vs-blue agent scrimmage on the tezcatlipoca
range. Work autonomously; report findings as you go. Do not ask questions unless
you hit a genuine GO/NO-GO gate failure.

## Ground truth (verified 2026-09-30, trust this over any doc that disagrees)

- Worktree: `/home/hna/dev/dawgsec/tezcatlipoca-testcomp` — branch
  `testcomp-cyberfield-2026-09-29`. Do ALL work here. Never touch
  `/home/hna/dev/dawgsec/tezcatlipoca` (main tree) or
  `/home/hna/dev/dawgsec/tezcatlipoca-loadtest-cr` (a parallel session may still be
  running a `loadtest-cr-a` deploy against the OLD cyberrange node .150 from it).
- Competition: `competitions/testcomp-7box-2026-09-29` — 1 team (identifier 120),
  6 managed boxes + manual pfSense. Engine VM (vmid 1000) and golden templates
  (1140 engine-template, 1150–1155 golden-dc01/win02/web01/mail01/db01/splunk01)
  are ALREADY built and sealed on cyberfield (10.0.0.193, node `pve`,
  datastore `hdrives-zfs`). Deploy state: phases 1–4 complete, stopped on purpose
  at the phase-5 boundary.
- **Engine mgmt IP is 10.0.0.252 (gateway 10.0.0.1), already set in the worktree
  `.env`. NEVER use 10.0.0.250** — that address is poisoned on the tailscale path
  (the `headscale-network-bridge` subnet router answers for it and never forwards
  to the node; packet-capture-proven). .245/.246 are also claimed.
- Python: plain `python3` works (toml/proxmoxer installed system-wide). All driver
  commands run from the worktree root.
- Secrets: `competitions/testcomp-7box-2026-09-29/.deploy_state.json` holds the
  generated comp credentials (admin/team/inject/box). It is gitignored — never
  print its values, never commit it.
- The user plays blue in person at some point. **Never enumerate or describe the
  planted misconfigurations** in chat or reports — they live in box_vulns.json;
  just reference counts/boxes if needed.
- pkill discipline: any pkill pattern MUST be scoped to the comp name
  (`create-competitio[n].*testcomp-7box-2026-09-29`), never bare.

## Step 0 — sync and sanity

```bash
cd /home/hna/dev/dawgsec/tezcatlipoca-testcomp
git status -sb && git fetch origin
git merge origin/main --no-edit   # picks up 5f114f5 routed-red mode (red01 real
                                  # source IP on 10.200.0.0/24 + verify --red-identity)
python3 -m unittest discover -s tests 2>&1 | tail -2
```

If the merge conflicts, resolve conservatively (the branch's template_ops.py /
constants.py / quotient fixes are live-found and must survive) and note it in the
final report. Confirm `.env` still has `TF_VAR_engine_mgmt_ip=10.0.0.252` and
`TF_VAR_engine_mgmt_gw=10.0.0.1` after the merge.

Read (before touching anything): `docs/usage-agents.md` (deploy-from-worktree +
resume semantics), `docs/pfsense-inpath-2026-09-28.md` (the pfSense runbook you
will follow verbatim), the routed-red section of `docs/scrim-harness.md`,
`docs/pfsense-rvb-2026-09-28-report.md` (the red+blue+pfSense precedent, including
the beacon-vs-pfSense trap), and `run-agent-scrim.py --help`.

## Step 1 — resume the deploy (phases 5–7)

```bash
python3 -u create-competition.py --competition testcomp-7box-2026-09-29 \
  --teams 1 --scoring-vmid 1000 --from-phase 5 --yes \
  2>&1 | tee /tmp/deploy-testcomp-phase567.log
```

This clones the 6 team boxes from the goldens (vmids 1400–1405), runs the final
plant, seeds team1, unpauses the engine, and creates the 12 injects. On failure:
resume with `--from-phase N` (max 2 repair cycles; if it still fails, stop and
report). Watch for the known trap: a planted box that wedges is NEVER restarted —
rebuild it instead (PAM/SSH-after-restart quirk, docs/known-issues.md).

## Step 2 — pfSense in-path (follow the runbook, team 120 only)

Per `docs/pfsense-inpath-2026-09-28.md`: transit bridge `vmbrW120`; full-clone
the pfSense template **vmid 956** (`pfsense-fix`) to a free vmid (suggest 1410);
net0→vmbrW120 / net1→vmbr120; per-team config via `pfsense/gen_pfsense_config.py`
(WAN `172.31.120.2/30`, LAN `192.168.120.1/24`, WAN pass rule `<network>lan</network>`);
serve the XML from the engine and fetch it via the pfSense console
(`qm sendkey` + screendump; temp IP on the unassigned LAN NIC; **never inject
config.xml host-side** — host OpenZFS makes the pool unmountable; never rename the
pool). Then the engine cutover: one reboot with all transit NICs, netplan
MAC-matched (`172.31.120.1/30`, route `192.168.120.0/24 via 172.31.120.2`, drop
the engine's `192.168.120.1`). `onboot=1` on the firewall.

Also add the **beacon C2 DNAT port-forward on the pfSense LAN** (docs/e2e-testing.md
§8.3): without it, red's raw-socket beacon check-ins die at the firewall — the
known gap from the pfsense-rvb run. If gen_pfsense_config.py can't render the
forward, add it through the console shell (`pfSsh.php`/config.xml edit + `fetch`)
and prove it with a check-in before moving on. If you truly cannot get C2 through,
note it and continue — the run is still valid, C2 health is then informational.

Verify in-path-ness: from the engine, the ONLY route to 192.168.120.0/24 is via
172.31.120.2, and scored ports answer through it.

## Step 3 — verify (GO/NO-GO)

```bash
python3 verify-competition.py competitions/testcomp-7box-2026-09-29 \
  --strict-services 2>&1 | tee /tmp/verify-testcomp.log
```

The engine rebooted in step 2, so if the round loop didn't resume, re-run with
`--fix-round-loop`. AD domain: pass `--windows-domain-validated` only after you
actually confirm domain join (verify output will tell you what it saw). ALL gates
must pass (logins, no_default_creds, isolation, misconfig, misconfig_survival,
services strict, pins_registered, plant_coverage, domains, injects). Failures:
fix and re-verify; do not proceed past a red gate.

## Step 4 — bad-auto coverage (GO/NO-GO)

From `/home/hna/dev/dawgsec/bad-auto` (export the worktree's `TF_VAR_proxmox_*`
trio first — `set -a; . ../tezcatlipoca-testcomp/.env; set +a`, minus the
TF_VAR_teams/boxes/scoring vars):

```bash
python3 -m badauto deploy --competition ../tezcatlipoca-testcomp/competitions/testcomp-7box-2026-09-29 \
  --red-vmid 999 --red-storage hdrives-zfs --red-ip 10.0.0.244 --red-gw 10.0.0.1
```

(If routed-red mode changed the red networking flags, follow the current docs —
the constants are: red01 vmid 999, storage hdrives-zfs.) No `--start`. Then ssh
to red01 and run the deterministic coverage pass for team1 — every scored pin
must flip DOWN and back UP on the scoreboard. Then
`python3 -m badauto destroy --competition ... --yes` (must match config.yaml).
Any pin that won't flip both ways = fix or re-pin before the event; a dead pin
means dead scoring for 3 hours.

## Step 5 — the 3-hour run

Prereqs: `OPENROUTER_API_KEY` in env (blue + red LLM calls) and
`BAuto_LLM_API_KEY` reachable for bad-auto (its own .env). Then, from the
worktree:

```bash
python3 -u run-agent-scrim.py --competition testcomp-7box-2026-09-29 \
  --teams 1 --duration-min 180 --blue-watchdog \
  --red-vmid 999 --red-storage hdrives-zfs --red-ip 10.0.0.244 --red-gw 10.0.0.1 \
  2>&1 | tee /tmp/scrim-testcomp-3h.log
```

Defaults are fine for models (openrouter `gpt-5.6-luna`, `--reasoning-effort
minimal`); raise `--reasoning-effort` only if the user asked. `--blue-watchdog`
keeps availability scoring alive through blue API outages — keep it on. The
harness pauses the engine, stages the blue workdirs, re-anchors inject clocks at
T0, and drives red01 through bad-auto. Known trap it handles for you: inject
re-anchor uses multipart UpdateInject (attachments not re-listed get deleted —
if an inject loses its attachment after re-anchor, that's this bug; report it).
Run in a tracked background terminal; poll the log every ~10 min; intervene only
on harness crashes, not on agent behavior. Blue gets ONE qlogin jar — never
concurrent logins with the monitor.

Inject timing note: the 12 injects close at T0+2h00m; the final hour is pure
availability + red pressure. That is intended.

## Step 6 — post-run

When the run ends: let the harness write its report (run-dir under the worktree),
then `--keep-range` semantics apply only if you passed it — you did NOT, so
**pass `--keep-range` in step 5** if you want the range preserved for the user to
inspect after the run (recommended: keep it; the user plays these boxes).
Teardown later is:
`python3 destroy-competition.py --competition testcomp-7box-2026-09-29 --full --end-of-competition`
from the worktree (add `--end-of-competition` because a frozen/post-run comp
refuses `--full` without it; `--skip-vm` logic for red01 is in bad-auto's own
destroy if needed).

Final report to the user (no misconfig details): deploy/verify/coverage outcomes,
final scoreboard (blue vs red points), service down-minutes per box, red action
highlights by tactic, inject submission count + scores, blue model misbehaviors,
C2-through-pfSense health, and the exact teardown command. Push any code/doc
changes made along the way to the branch (never secrets, never .env/state files).
Update the tezcatlipoca project memory with anything live-found that future runs
need.
