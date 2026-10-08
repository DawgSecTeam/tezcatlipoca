# Transfer prompt — scrim-two rehearsal on Cyber Realm (.150): close every observability gap

Copy everything below the line into a fresh session.

You are running a diagnostic dress rehearsal whose goal is NOT a clean scoreboard — it is to
surface every weak point in the stack (infra, scoring, red agent, blue agent, harness) and
record enough evidence to make each future scrim reproducible. Work autonomously; do not ask
questions unless you hit a genuine GO/NO-GO failure. Read the INCIDENT section before touching
the node's networking.

## Ground truth (verified 2026-10-07)

- Repo: /home/hna/dev/dawgsec/tezcatlipoca — main @ 2860958, vendor/nakon @ 22360ba. Use
  `.venv/bin/python` (3.14) for every driver command; terraform is system-wide.
- bad-auto: /home/hna/dev/dawgsec/bad-auto — main @ d110d69 + uncommitted config.yaml changes
  (red_storage=hdd, reasoning_effort removed — this server's chat template REJECTS
  reasoning_effort; do not re-add it for local llama.cpp endpoints). badauto runs on system
  python3 (pyyaml installed --user).
- Proxmox: realm.hnasheralneam.dev / 10.0.0.150, node proxmox, datastores: ssd, local-lvm,
  hdd, local, wkshp-pool (NO hdrives-zfs). Root SSH key: ~/dev/dawgsec/tezcatlipoca/proxmox.
  API token in repo .env (never print).
- .env already set for this node: TF_VAR_template_vm_id=955 / team_identifiers=120 /
  engine_mgmt_ip=10.0.0.252 / engine_mgmt_gw=10.0.0.1. Do not reintroduce the 9088 ghost trap.
- Secrets: competitions/scrim-one/.deploy_state.json (gitignored). Never print values, never
  commit.
- Never enumerate or describe the planted misconfigurations in chat (box_vulns.json); counts
  and boxes only. The owner may play blue.
- Deployed range (do NOT redeploy): engine VMID 1000 @ 10.0.0.252; engine template 1140;
  goldens 1150–1155; team boxes 1400–1405 on vmbr120 (192.168.120.0/24, team 120);
  **fw01 VMID 1410 in-path** (net0=vmbrW120 WAN 172.31.120.2/30, net1=vmbr120 LAN
  192.168.120.1/24; tz-base snapshot taken post-cutover; `firewalls_bootstrapped: true` in
  the state file). Engine transit NIC = ens20, addressed by /etc/netplan/60-team-ifaces.yaml.
- Last run: competitions/scrim-one/.automated-tests/run-b6c2db3e (run 3 completed rc=0,
  NOT SEALED — its judgement sections are the starting evidence). Harness bugs fixed this
  week (scrim/blue_watchdog.py dict-form box_services crash; coverage must run ON red01) —
  both documented in the skill reference references/in-path-firewall.md.

## Known-open questions — each is a required output of this rehearsal

### A. Red (bad-auto) behavior
1. **Windows footholds: 0/2 all last run.** Reproduce and diagnose: does the local-user /
   empty-password chain even reach dc01/win02 through the in-path firewall, or is it tactic
   selection, AD auth (NTLM over WinRM/SMB through transit?), or the box creds in the intel
   bundle? Get a decision trace (`events.jsonl`) that names the first failing step.
2. **max_simultaneous_down was 1 (gate wants ≥4).** Check the pacing config vs what actually
   ran: burst windows, `max_concurrent_down_*`, and whether the stall (1 last run) was
   LLM-latency serialization on the shared flash-next endpoint. If capacity is the cause,
   record the real prompt queue depth (llama.cpp /slots during the run).
3. **Beacon C2 through the firewall.** fw LAN DNAT 4470/udp → 10.0.0.244 exists in
   config-team120.xml but beacon check-ins were never exercised live. Run `badauto
   beaconfire` against the comp and capture plant/check-in/echo/takedown/restore rows.
4. **The one stall + 3 evictions.** Classify: endpoint contention vs guardrail block vs
   session clobber. Blue and red on separate endpoints last run — keep that.
5. **`score/tcp` and `Enable WinRM` coverage artifacts** (see skill ref). Verify WinRM stays
   UP under load (dc01's WRMR rules were widened to Any on 2026-10-07); confirm whether a
   port-block takedown path exists for score_only pins.

### B. Blue agent
6. **Blue timeout (1/cycle-rate).** Measure where the cycle time goes on qwen3.8-27b:
   prompt size (scoreboard + 12 injects + notebook) vs inference. If prompt-bound, trim the
   cycle prompt; record the choice in the report.
7. **Restoration quality: 2 self-restorations vs 2 watchdog restores.** Did blue actually
   diagnose nginx on web01, or did it flail until the watchdog caught it? Diff the notebook
   entries against watchdog.log RESTORED lines.
8. **Inject submissions:** 11/12 worked. Find which one blue never closed and why
   (clock re-anchoring? submission format?). Note injects CLOSE on a rerun — re-anchor clocks
   at T0 like run-agent-scrim does, and do not trust an old run's offsets.
9. **Blue under deception:** does blue notice scoring arrives from 172.31.120.1 (transit) and
   treat the firewall as infra, not compromise? One blue cycle should be examined for
   firewall-awareness in its notebook.

### C. Infra / scoring under load
10. **Engine cold-boot persistence.** The netplan/MASQUERADE/DOCKER-USER rules were re-apply
    by hand after the NIC cold boot. Make them durable (systemd unit or netplan-embedded) or
    file the exact re-apply sequence into the runbook; then TEST: qm stop/start 1000, and
    prove scoring resumes in-path with zero manual repair (this is what a comp-day power
    blip will do). `--fix-round-loop` blind spot: verify reads paused-as-PASS — unpause via
    POST /api/competition/start + /api/engine/pause and confirm 'loop advancing'.
11. **apt-cacher DNAT (3142) through the fw under real apt load**: rebuild one box
    (redeploy_rebuild_ops) and confirm its apt updates flow via 192.168.120.1:3142 →
    172.31.120.1:3142 with no DNS stalls.
12. **fw01 failure drill:** stop 1410, confirm scoring flips ALL team services DOWN cleanly
    (not partial/stale), start 1410, confirm convergence WITHOUT touching the engine. Record
    convergence time — that's the comp-day blast-radius answer.
13. **plant-integrity WARNING: ldap-signing-off-win FAILED on dc01** every run. Decide:
    fix the plant (one-off nakon pass) or prune the pin; do not leave it WARNING-state.
14. **Coverage gaps to re-prove in-path:** re-run badauto coverage ON red01 (state file
    staged to /var/lib/bad-auto/intel/) and get 13/13 or a written artifact-explanation per
    exception.

### D. Harness / reproducibility
15. **Endpoint roster honesty:** probe before rostering (`/v1/models`, `/slots`); the
    originally-planned :8083/:8888 servers on 100.64.0.19 were DOWN. Record which endpoint
    served which agent, model, and ctx, and GPU state (http://localhost:8081/api/gpu) at
    start AND end. vLLM(:8000) and llama.cpp(:8080) share one GPU, one at a time — never
    co-roster both sides onto hosts that serialize.
16. **Fill + seal the run-3 report** (`test-artifacts.py verify scrim-one run-b6c2db3e
    --seal`) using this rehearsal's findings, so the diff between the two runs is sealed
    evidence.
17. **Artifact collection:** last run showed absent=8, unreachable=1 (RED-TEAM.md — report
    file was root-owned on red01). Fix the report perms collection path in
    scrim/artifacts or badauto, or note the workaround, so RED-TEAM.md always lands.

## Run plan

1. Snapshot current state (VMs, rounds, scoreboard JSON) into logs/ before anything.
2. Quick gates: verify-competition --strict-services must PASS before you start (unpause
   first if paused).
3. Infra drills first (items 10, 11, 12) — they change the range; do them while no agent is
   playing. Restore to a clean, in-path-verified, 12/12-UP range after each.
4. Red diagnostics (items 1–5) via short `badauto run --dry-run`/`--once` cycles + a focused
   30-min live red run with events.jsonl pulled every 5 min.
5. Full 3-hour scrim, same command as run 3 EXCEPT: pick endpoints by probing fresh, and
   raise blue observability (leave all harness logging on):
   `run-agent-scrim.py --competition scrim-one --teams 1 --duration-min 180 --blue-watchdog
   --keep-range --skip-deploy --reasoning-effort '' --llm-base-url <red-ep> --red-model <m>
   --blue-base-url <blue-ep> --blue-model <m>` — weaker model to blue (owner's rule).
   Run in a tracked background terminal; watch for worker-thread DIED lines and fix/relaunch.
6. Post-run: coverage re-sweep on red01 (item 14), beaconfire (item 3), then fill and seal
   BOTH reports (item 16).
7. Deliver a per-item findings table (item number → verdict → evidence path). Do not tear
   down the range; goldens, team boxes, fw01 stay for the owner.

## Hard rules (unchanged, from the 2026-10-07 incident)

- NEVER `systemctl restart networking` or any full network reload on 10.0.0.150 — it orphans
  every running VM's bridge leg. Bridges via `pvesh create /nodes/proxmox/network` only.
- If legs orphan: re-enrol tap<vmid>i<nic> / fwpr<vmid>p<nic> by NAME (pve-host-network-ops
  skill, scripts/recover-vm-legs.py). No VM was ever stopped by this; keep it that way.
- `qm guest ping` doesn't exist; test QGA with `qm guest exec <vmid> -- true`. This PVE's
  snapshot API wants `snapname` — use range_ops.take_snapshot. Guest-agent IP discovery
  returns null on .150 — never rely on terraform IP discovery there.
- Do not reboot the physical host. The owner may be out.
- Service VMs are DHCP — IPs drift; use infra-registry, never VM-number assumptions.
