# Full-dress rehearsal prompt — paste into a fresh agent session

Copy everything below the line into the agent. Run it 1–2 days BEFORE the
practice so there is time to fix what it finds. Budget a full day.

---

You are running the FINAL full-dress rehearsal of the red-vs-blue agent scrim
before the first real practice. Everything must work end to end with the local
AI endpoints only — zero OpenRouter spend. Your job is not just to run it, but
to break it first and fix what breaks. Be exhaustive; the practice is next
weekend and this must be flawless.

## Context (read first)

- Range driver: /home/hna/dev/dawgsec/tezcatlipoca (this repo). Red agent:
  /home/hna/dev/dawgsec/bad-auto (sibling repo, ships live from its checkout).
- Orchestrator: run-agent-scrim.py — authors, deploys, verifies, feeds blues,
  deploys red01, monitors, captures, tears down.
- Round-3 debrief + both fix rounds: /home/hna/dev/dawgsec/scrim-runs/
  agent-scrim-2026-09-17c/FINDINGS.md (incl. Resolution log + 2026-09-20
  addendum: the scoreboard had TWO stacked causes — session clobbering AND
  /api/services/<subnet-id> returning Forbidden; both fixed and verified live
  on e2e-2026-09-19).
- Local LLM: llama.cpp serving qwen3.6-35b-a3b at http://100.64.0.9:8080/v1
  (a tailscale machine, NOT this host — this host is 100.64.0.11). Blues reach
  it directly (proven). red01 (10.0.0.198, node LAN, no tailscale) needs a
  probe for a LAN-reachable address, else a tunnel (Phase 3).
- All 2026-09-19/20 fixes are committed on main in both repos: shared cookie
  jars, team-ID mapping, compose-up 600 s, opencode retry + 60 k local ctx,
  keyless local endpoints, bad-auto red01 state archive + on-box unit
  resolution (apache→apache2).

## Phase 0 — pre-flight gates (do not skip; fix, don't route around)

1. Node health: `pmx list` + one `pmx exec <vm> -- true` on a running guest
   (env file: /home/hna/dev/dawgsec/huitzilopochtli/tests/proxmox/.env). If
   exec read-times out, retest with a longer proxmoxer timeout
   (ProxmoxAPI(..., timeout=90)) before declaring a wedge — the 5 s default
   cap has false-alarmed before.
2. No leftover range VMs in vmid 1000–1300 except workshop/template VMs.
3. Catalog: `curl -s http://10.0.0.121:3000/` → 200, and vendor/nakon/.env
   host=10.0.0.121. vulndb is pinned static; drift means the pin broke —
   investigate, don't sed to a new IP.
4. Local LLM: `curl -s http://100.64.0.9:8080/v1/models` lists qwen3.6-35b-a3b,
   and a trivial chat completion succeeds. Also measure a ~4 k-token prompt's
   latency — blues' cycle prompts are big and the 1500 s cycle timeout must
   hold.
5. red01 reachability of the endpoint: from the operator, `tailscale status`
   to identify 100.64.0.9's hostname, then check whether that box answers on a
   10.0.0.x/192.168.1.x address too (probe `curl http://<addr>:8080/v1/models`
   from a node-LAN vantage, e.g. the scoring engine once deployed or via the
   Proxmox guest agent on any 10.0.0.x VM). Record the outcome for Phase 3.
6. Git: both repos clean and pushed; `python3 -m py_compile` the pipeline
   modules; `python3 create-competition.py --help` and
   `python3 run-agent-scrim.py --help` import cleanly.

## Phase 1 — deploy a fresh competition, with a deliberate crash drill

- Author competitions/scrim-dress-2026-09-2X from competitions/agent-scrim-
  2026-09-17c (stage_author semantics: skip RUNTIME_FILES, sub-*, LOG.md,
  .nakon-domain-*, .phase6-swept; rewrite Compfile name).
- Run `python3 create-competition.py --competition scrim-dress-... --teams 2
  --yes` in the background with a full log.
- CRASH DRILL: once phase 3 completes, kill the deploy process mid-phase-4,
  then resume with --from-phase 4. This exercises the resume/secret-persistence
  path on purpose. The deploy may still hit known environmental flakes —
  handle per playbook: compose-up >60 s is fixed (600 s); apt 404 on a
  catalog-pinned package self-heals after the box's apt-daily refresh (retry);
  fresh Windows clones can need >90 s before the guest agent answers (resume);
  qemu-server lock timeouts are usually transient (recheck state, resume);
  team2 Linux clones may come up without IPv4 (cloud-init race) — repair via
  guest agent (address + default route; on the Debian app01 prefer a
  systemd-networkd .network file with KeepConfiguration — ifupdown loses the
  address on any carrier blip).

## Phase 2 — live-engine smoke (the checks that caught real bugs)

With the range up, from the operator:
1. `python3 verify-competition.py competitions/scrim-dress-... --engine-ip
   10.0.0.92 --admin-password <from .deploy_state.json>` → every check PASS.
2. Cookie-jar + scoreboard smoke (import run-agent-scrim via importlib): call
   status_text for team1 and team2 → both render 8 UP/DOWN lines, NO
   "unreachable"; team_down returns False; then log in out-of-band as team1
   (curl) to clobber the jar, call status_text again → must self-heal and
   still render. Any Forbidden here is a showstopper — fix before Phase 3.
3. Blues' helper set: materialize a workdir (stage_blues semantics), run
   ./score.py and ./submit-inject against the engine (submit a throwaway
   inject), confirm both work and heal after a clobber.

## Phase 3 — the dress rehearsal itself (local endpoints only)

- Command shape:
  `python3 run-agent-scrim.py --competition scrim-dress-... --duration-min 90
   --teams 2 --skip-deploy --blue-base-url http://100.64.0.9:8080/v1
   --blue-model qwen3.6-35b-a3b --red-model qwen3.6-35b-a3b --reasoning-effort ''`
  (reasoning-effort '' — qwen has no effort knob; the opencode.jsonc for local
  blues already uses ctx 60000).
- Red on local: stage_red writes bad-auto's llm.base_url from --llm-base-url.
  If Phase 0.5 found a LAN-reachable address for the llama box, pass
  `--llm-base-url http://<that-addr>:8080/v1`. Otherwise bring up a relay
  BEFORE stage_red so both operator-side (validate-llm/dry-run) and red01-side
  (director) can use the SAME URL: socat TCP-LISTEN:8180,fork,reuseaddr
  TCP:100.64.0.9:8080 on the operator, plus `ssh -N -R 8180:127.0.0.1:8180
  -i proxmox sysadmin@10.0.0.198` (keep it alive all event, autossh or a
  supervised loop), then pass `--llm-base-url http://localhost:8180/v1`.
  Verify from red01: curl the base URL through the tunnel before starting.
- Run the orchestrator in the background with a full log; do not babysit each
  cycle, but check in at least every 15 minutes.
- HARD GATES (every one must hold; a miss is a finding to fix and re-run):
  - Every monitor.log snapshot shows a real scoreboard for BOTH teams — the
    string "scoreboard unreachable" must appear ZERO times all event.
  - ≥8 orchestrator blue cycles complete rc=0 with real transcripts (local
    qwen turns run 30–60 s; the 1500 s cycle timeout must never trip).
  - ≥1 inject submitted via ./submit-inject and visible on the scoreboard.
  - red events.jsonl pulled at every monitor snapshot AND at teardown, non-
    empty; red shows the full decision chain (footholds, impact attempts —
    impact_service must resolve apache→apache2 on app01: zero rc=5
    wrong-unit failures in events.jsonl).
  - Blue restoration behavior observed at least once (red takes a service
    down; a later snapshot shows a blue restore attempt).
  - The shared local endpoint survives red+blue concurrency (no slot
    starvation death spiral; if cycles stall, measure queue depth and
    document — do not silently widen the timeout).
  - stage_capture writes parsed final-services JSON for both teams (no null,
    no "error"), and teardown completes exit 0 (red01 destroyed, range
    destroyed).

## Phase 4 — regression checklist (fast, after teardown)

- /tmp/jar.team1 and /tmp/jar.team2 semantics: one more clobber/heal cycle on
  the mock (python3 /tmp/mock_quotient_test.py if present, else rebuild it —
  it must enforce the team-ID semantics that caught the real bug).
- bad-auto: `python3 -m badauto run --once --dry-run --competition
  competitions/scrim-dress-...` green; impact_service alias table covers
  apache/bind/mysql/exim.
- Confirm both repos still clean/pushed at the end; list any straggler files.

## Phase 5 — report

Write scrim-runs/scrim-dress-.../FINDINGS.md: what passed, what broke, what
you fixed vs. what needs a human decision, and a GO / NO-GO verdict for the
practice with the top 3 risks. Update the auto-memory files with anything
load-bearing. Leave the range DOWN unless the operator said otherwise.

## Guardrails

- The Proxmox node is shared with the workshop portal: never touch workshop-*
  VMs, never run destructive node operations, keep everything on the hdd pool.
- No OpenRouter calls anywhere in this rehearsal — if a cloud key would be
  used, stop and record the gap instead.
- The scoring engine and team boxes are fair game (that's the exercise); the
  Proxmox node, the vulndb VM, and the operator host are not.
- Report outcomes faithfully: a gate that failed is a failed gate, not a
  partial pass.
