# Tezcatlipoca + WRCCDC (swbar) practice-competition setup plan

Written 2026-09-24 by bossagent. Range status at writing: all 10 `challenge-swbar-*`
VMs (tagged `dev`) running, tlaloc brain-tested against the target set.

## What exists today

- **WRCCDC range** = Star Wars Bar range (see infra-registry
  `services/star-wars-bar-range.md`): 10 VMs on `untrustedbr`, subnets
  192.168.220.0/24 ("LAN", pfSense routooine = gateway .220.2) and
  192.168.100.0/24 (WAN side). Boxes are scenario-baked (NOT nakon-planted):
  Kamino/DeathStar intentionally unpatched. Master scenario password
  `iamy0urf4ther` planted everywhere (rotate on any reuse).
  IMPORTANT L2 fact: both subnets live on the SAME bridge (untrustedbr, portless).
  A VM with a .220.x static IP has direct L2 access to all swbar boxes — no
  routing or NAT needed; the .100.x / .220.x split is only IP + pfSense policy.
- **tlaloc (bad-auto)** at `~/dev/dawgsec/bad-auto`: Quotient sensor is OPTIONAL
  (`quotient.base_url: null` skips it). Targets/ROE derive from a competition
  dir: `teams.json` → subnet hardcoded `192.168.<identifier>.0/24`;
  `boxes.json` → IP `<subnet>.<last_octet>`; Windows = `"win"` in template name.
  red01 deploy (`badauto deploy`) assumes the tezcatlipoca engine (route via
  engine + MASQ) — needs a swbar topology profile.

## Already done (2026-09-24)

1. All 10 swbar VMs started (pfSense 376 first, DC 374, then the rest).
2. Competition dir `~/dev/dawgsec/tezcatlipoca/competitions/wrccdc-swbar-dev/`:
   teams.json (team1/id 220 → 192.168.220.0/24), boxes.json (7 in-ROE boxes:
   sing3po .134, wookie .20, ashla .202, bogan .203, tython .201, deathstar
   .105, kamino .11), users.json, .deploy_state.json (scenario password),
   nakon-config.json (per-machine vuln hints from the recon inventory).
3. `bad-auto/config-swbar.yaml`: local qwen3.6-35b endpoint (10.0.0.143:8080/v1,
   **max_tokens 16000** — the reason-model empty-content pitfall), intel nakon,
   Quotient null, beacon disabled.
4. Tests: `python3 -m unittest discover -s tests` → 150 OK. `validate-llm` →
   PASS. `run --once --dry-run` → PASS (7 targets, correct ROE, sensible LLM
   opening decision: cred_spray on .134). tlaloc is ready for live use against
   this range.

## Plan

### Phase 0 — baseline & guardrails (~30 min)
- Snapshot all 10 swbar VMs now → clean per-practice reset point (restore ≈
  seconds vs the nakon re-seed path if state burns).
- Verify live IPs/agents per box; corellia (373) guest agent is known-dead —
  probe manually (it's a range filler; decide if it stays in/out of scoring).
- Keep VMIDs until the range is retired; never `qm shutdown` 373 (use `qm stop`).

### Phase 1 — scoring: Quotient engine on the swbar LAN (1–2 days)
- Hand-author `event.conf` + credlists reflecting the REAL swbar services
  (source: `~/wrccdc-recon/*.md` + VulnDB ids 163–188): WordPress (wookie),
  Karaoke+Cockpit (sing3po), Mailu/Samba/NFS (bogan), vsftpd/webroot-CGI
  (ashla), K3s/POS (tython), AD/DNS/IIS (deathstar), kiosk/uvicorn (kamino),
  MailEnable/WingFTP (tat10ine), pfSense DNS/DHCP.
- Deploy engine VM with one NIC on untrustedbr, static 192.168.220.x: bootstrap
  Docker + Quotient (reuse `engine_ops.bootstrap_scoring_engine()`), push the
  hand-authored event.conf. Do NOT run create-competition.py (it would clone
  fresh team boxes and nakon-plant them, burning the scenario).
- Verify: scoring rounds all-green on the untouched range; scoreboard reachable
  from 10.0.0.x; then `quotient.base_url` goes into config-swbar.yaml so tlaloc
  gets the scoreboard sensor.
- Blue access (decision needed): jump host VM with a .220.x NIC on untrustedbr,
  published via Guacamole or over tailscale SSH/RDP. Recommend one jump host,
  not per-defender VMs.

### Phase 2 — red01 with a swbar topology (half day)
- Extend `badauto/deploy` with a swbar profile: clone `ubuntu24.04-fix` (955),
  net0 → untrustedbr static 192.168.220.240 gw .220.2 (attack NIC, in-ROE),
  net1 → vmbr0 DHCP (control plane: LLM endpoint + SSH). Policy routing:
  default via net1, `192.168.220.0/24 via 192.168.220.2` via net0 — so attack
  traffic sources .220.240 and the control plane never crosses the range.
  Skip engine NAT/DNAT entirely (no engine exists).
- Deploy + `run` with the already-tested `config-swbar.yaml`.

### Phase 3 — beacon C2 channel (optional, after first round)
- pfSense port-forward .220.2:4470/udp → red01 (adapts the engine-DNAT design
  the beacon expects), then `beacon.enabled: true`. Verify `beacon_plant` /
  `beacon_check` / `beacon_run` once red holds root. Rotate the beacon
  identity/win service names per event so blue IOC lists stay fresh.

### Phase 4 — run the practice competition
- 60–180 min event: Quotient clock + injects, blues harden, tlaloc red-teams
  with pacing caps. Post-event: `badauto report` (timeline, footholds, IOCs)
  + Quotient final score → blue post-mortem.
- Between practices: rollback the Phase-0 snapshots, re-verify event.conf
  checks are green, restart the clock. Rotate the scenario password if blues
  learned it.

## ROE / safety notes
- Red ROE = **192.168.220.0/24 only** (tlaloc enforces via the teams.json
  allowlist). tat10ine (.100.150) and pfSense perimeter drills are MANUAL red
  work — untrustedbr's .100.x segment hosts 20+ unrelated live workshop VMs and
  cannot be subnet-allowlisted safely.
- pfSense/corellia are not in boxes.json → tlaloc never targets them
  (pfSense is red's own gateway; a self-DoS).
- Engine VM, vulndb (10.0.0.121), LLM endpoint (10.0.0.143) stay off-ROE.

## Decisions needed from Leone
1. Blue-team access path (Guacamole jump host vs tailscale direct).
2. Scoring source for v1: Quotient service-uptime (recommended) vs
   huitzilopochtli-style per-box hardening checks (more authoring, more
   WRCCDC-realistic check stories).
3. Take the Phase-0 snapshots now? (yes recommended)
