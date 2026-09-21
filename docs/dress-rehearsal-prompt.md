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
  bad-auto's GitHub home is DawgSecTeam/tlaloc — clone it to exactly that path
  (`git clone git@github.com:DawgSecTeam/tlaloc.git ~/dev/dawgsec/bad-auto`);
  run-agent-scrim hardcodes the sibling `../bad-auto` path, and the system
  python3.11 can run badauto (stdlib + requests/yaml/paramiko, all present).
- Orchestrator: run-agent-scrim.py — authors, deploys, verifies, feeds blues,
  deploys red01, monitors, captures, tears down.
- Round-3 debrief + both fix rounds: the scrim-runs/ tree does NOT exist on
  this host (agent-scrim-2026-09-17c/FINDINGS.md is gone — the e2e run dirs
  were cleaned). The fix history is in git log instead: f71a80a (shared cookie
  jars), a98ee49 (services by internal team ID, not subnet identifier — the
  Forbidden fix), 63b9c31 (compose-up 600 s), 55136c2 (keyless local endpoints).
  The scoreboard had TWO stacked causes — session clobbering AND
  /api/services/<subnet-id> returning Forbidden; both fixed and verified live.
- Local LLM: llama.cpp serving qwen3.6-35b-a3b at http://100.64.0.9:8080/v1.
  **This operator host IS that box** ("ai-server", tailscale 100.64.0.9, node
  LAN 10.0.0.143) — the old "NOT this host, this is 100.64.0.11" note was
  wrong. llama-server listens on 0.0.0.0:8080 (4 slots, 262k ctx), so it is
  ALSO reachable from the node LAN at http://10.0.0.143:8080/v1 — verified
  200 from a node-LAN guest (VM115). red01 (10.0.0.198, node LAN, no
  tailscale) can therefore reach the endpoint DIRECTLY: use
  `--llm-base-url http://10.0.0.143:8080/v1` and skip the socat/ssh tunnel
  entirely. Re-probe once at Phase 0.5 in case the LAN IP drifted (DHCP).
- All 2026-09-19/20 fixes are committed on main in both repos: shared cookie
  jars, team-ID mapping, compose-up 600 s, opencode retry + 60 k local ctx,
  keyless local endpoints, bad-auto red01 state archive + on-box unit
  resolution (apache→apache2).

- Interpreter: EVERYTHING driver-side runs under the repo venv —
  `.venv/bin/python create-competition.py ...` etc. The system python3 is
  3.11 and dies on `import toml` (and on vendor/nakon's f-string syntax);
  `.venv/bin/python` (3.14) has requests/toml/dotenv/pyyaml/paramiko.
  badauto is the exception — it runs fine on system python3 and the
  orchestrator invokes it as `python3`; keep it that way (its deps are
  apt-install-class).
- `pmx` is NOT installed anywhere on this host. Phase 0.1 must use the
  huitzilopochtli helper directly:
  `from tests.proxmox.proxmox_helper import get_proxmox_client, guest_exec`
  (run from ~/dev/dawgsec/huitzilopochtli, which loads
  tests/proxmox/.env) or plain proxmoxer with that .env. The proxmoxer
  idiom is `.agent("exec").post(command=[...])` + poll
  `.agent("exec-status").get(pid=...)` — NOT `agent.exec.run(inputdata=...)`.

## Phase 0 — pre-flight gates (do not skip; fix, don't route around)

1. Node health: `pmx list` + one `pmx exec <vm> -- true` on a running guest
   (see Context: pmx doesn't exist — use the proxmox_helper idiom above). If
   exec read-times out, retest with a longer proxmoxer timeout
   (ProxmoxAPI(..., timeout=90)) before declaring a wedge — the 5 s default
   cap has false-alarmed before.
2. No leftover range VMs in vmid 1000–1300 except workshop/template VMs.
3. Catalog: `curl -s http://10.0.0.121:3000/` → 200, and vendor/nakon/.env
   host=10.0.0.121. VERIFIED 2026-09-20: the vulndb VM is named "vulndb"
   (VMID 115) and its ens18 address IS 10.0.0.121 — the infra-registry entry
   saying 10.0.0.119 is STALE (that IP no longer answers; do NOT sed the .env
   back to 119). 10.0.0.121 hosts the catalog (234 configs; `nakon catalog
   list` green through the venv). Registry note to fix:
   infra-registry/projects/vulndb-ui.md.
4. Local LLM: `curl -s http://100.64.0.9:8080/v1/models` lists qwen3.6-35b-a3b,
   and a trivial chat completion succeeds. Also measure a ~4 k-token prompt's
   latency — blues' cycle prompts are big and the 1500 s cycle timeout must
   hold. Measured 2026-09-20: 2.4 k-token prompt → 403 completion tokens
   (+1.5 k reasoning) in 8.1 s idle — no timeout risk; note qwen burns
   ~1.5 k reasoning tokens before content (bad-auto llm.py already falls back
   to reasoning_content; max_tokens 1024–4096 is adequate).
5. red01 reachability of the endpoint: RESOLVED 2026-09-20 — 100.64.0.9 is
   THIS operator box ("ai-server"), and it answers on the node LAN:
   `http://10.0.0.143:8080/v1/models` → 200, probed via the Proxmox guest
   agent from a node-LAN VM (VM115). red01 needs no tunnel; Phase 3 uses
   --llm-base-url http://10.0.0.143:8080/v1. Re-run the probe at rehearsal
   time (dhcpcd may have moved 143).
6. Git: both repos clean and pushed (bad-auto remote =
   git@github.com:DawgSecTeam/tlaloc.git, branch main);
   `.venv/bin/python -m py_compile` the pipeline modules;
   `.venv/bin/python create-competition.py --help` and
   `.venv/bin/python run-agent-scrim.py --help` import cleanly (system
   python3 CANNOT — see Interpreter note above).

## Phase 1 — deploy a fresh competition, with a deliberate crash drill

- Author competitions/scrim-dress-2026-09-2X from competitions/agent-scrim-
  2026-09-17b — the ONLY agent-scrim comp that exists (17c was never git-
  tracked and is gone; run-agent-scrim's DEFAULT_TEMPLATE pointed at the also-
  gone agent-scrim-2026-09-16 — repinned to 17b 2026-09-20).
  stage_author semantics: skip RUNTIME_FILES, sub-*, LOG.md, .nakon-domain-*,
  .phase6-swept; rewrite Compfile name. Authoring standalone via CLI is not
  wired (run-agent-scrim --new crashes in creds_from_files if you add
  --skip-deploy), so author via importlib, THEN deploy directly. Run from
  the repo root (cwd matters for the relative template path):

  ```
  .venv/bin/python - <<'EOF'
  import importlib.util, argparse
  spec = importlib.util.spec_from_file_location("scrim", "run-agent-scrim.py")
  m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
  m.stage_author(argparse.Namespace(new="scrim-dress-2026-09-2X",
                                    from_template="competitions/agent-scrim-2026-09-17b"))
  EOF
  ```
- Optionally re-vet pins first (green on 2026-09-20, 0 errors): from
  vendor/nakon run `.venv/bin/python -m nakon catalog check --boxes-json
  ../../competitions/scrim-dress-.../boxes.json --box-vulns ... --box-services
  ... --strict --json` against the NEW comp dir after authoring.
- Run `.venv/bin/python create-competition.py --competition scrim-dress-...
  --teams 2 --yes` in the background with a full log (venv python, NOT
  system python3).
- CRASH DRILL: once phase 3 completes, kill the deploy process mid-phase-4,
  then resume with --from-phase 4. This exercises the resume/secret-persistence
  path on purpose. The deploy may still hit known environmental flakes —
  handle per playbook: compose-up >60 s is fixed (600 s); apt 404 on a
  catalog-pinned package self-heals after the box's apt-daily refresh (retry);
  fresh Windows clones can need >90 s before the guest agent answers (resume);
  qemu-server lock timeouts are usually transient (recheck state, resume);
  team2 Linux clones may come up without IPv4 (cloud-init race) —
  clone_ops.ensure_cloned_network now auto-repairs this after the start loop
  (guest-agent repair: re-add address + default route; persists a
  systemd-networkd .network with KeepConfiguration plus an ifupdown stanza —
  the exact .network body lives in clone_ops._repair_box_network). If you see
  its WARNING line, fix the box by hand before continuing.

## Phase 2 — live-engine smoke (the checks that caught real bugs)

With the range up, from the operator:
1. `.venv/bin/python verify-competition.py competitions/scrim-dress-...
   --engine-ip <read from competitions/<id>/credentials.txt — 10.0.0.92 was
   the 17c-run value, do not hardcode> --admin-password <from
   .deploy_state.json>` → every check PASS.
2. Cookie-jar + scoreboard smoke (import run-agent-scrim via importlib): call
   status_text for team1 and team2 → both render 8 UP/DOWN lines, NO
   "unreachable"; team_down returns False; then log in out-of-band as team1
   (curl) to clobber the jar, call status_text again → must self-heal and
   still render. Any Forbidden here is a showstopper — fix before Phase 3.
3. Blues' helper set: materialize a workdir (stage_blues semantics), run
   ./score.py and ./submit-inject against the engine (submit a throwaway
   inject), confirm both work and heal after a clobber.

## Phase 3 — the dress rehearsal itself (local endpoints only)

- Command shape (venv python; opencode on PATH):
  `PATH=$HOME/.opencode/bin:$PATH .venv/bin/python run-agent-scrim.py
   --competition scrim-dress-... --duration-min 90 --teams 2 --skip-deploy
   --blue-base-url http://100.64.0.9:8080/v1 --blue-model qwen3.6-35b-a3b
   --red-model qwen3.6-35b-a3b --reasoning-effort ''`
  (reasoning-effort '' — qwen has no effort knob; the default is "minimal"
  and must be overridden; the opencode.jsonc for local blues already uses
  ctx 60000).
- Red on local: stage_red writes bad-auto's llm.base_url from --llm-base-url.
  Phase 0.5 CONFIRMED a LAN-reachable address (this operator box): pass
  `--llm-base-url http://10.0.0.143:8080/v1` (re-check `ip -4 addr` at run
  time in case DHCP moved the box) and SKIP the relay entirely. Fallback only
  if that probe fails: bring up a relay BEFORE stage_red so both
  operator-side (validate-llm/dry-run) and red01-side (director) can use the
  SAME URL: socat TCP-LISTEN:8180,fork,reuseaddr TCP:100.64.0.9:8080 on the
  operator, plus `ssh -N -R 8180:127.0.0.1:8180 -i proxmox
  sysadmin@10.0.0.198` (keep it alive all event, autossh or a supervised
  loop), then pass `--llm-base-url http://localhost:8180/v1`.
  Verify from red01: curl the base URL through the tunnel before starting.
- bad-auto side-gates before the run: write bad-auto/.env with
  `BAuto_LLM_API_KEY=local` (badauto's validate-llm hard-fails on an unset
  key even though llama.cpp ignores the value — run-agent-scrim only injects
  a key into ITS subprocess env, and a fresh clone has no .env); run
  `BAuto_STATE_DIR=/tmp/ba python3 -m badauto validate-llm` and confirm PASS;
  `python3 -m unittest discover -s tests` green (52 tests as of 2026-09-20);
  the Phase 4 `run --once --dry-run --competition <dir>` invocation is valid
  but must add `--state-dir /tmp/ba` (or BAuto_STATE_DIR) on the operator —
  the default /var/lib/bad-auto is root-owned and not writable.
- opencode (blue runner) is at ~/.opencode/bin/opencode — NOT on PATH for a
  bare shell or a nohup'd background session. Launch the orchestrator with
  `PATH=$HOME/.opencode/bin:$PATH ...` or blues fail to boot.
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
  - `python3 scrim-report.py /home/hna/dev/dawgsec/scrim-runs/scrim-dress-...`
    runs clean and its INTERACTION.md is scored against
    docs/rehearsal-gates.md — the interaction score (restorations + re-kill
    reactions + injects + eradication), not red's kill log, decides GO/NO-GO.
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
- bad-auto: `BAuto_STATE_DIR=/tmp/ba python3 -m badauto run --once --dry-run
  --competition ../tezcatlipoca/competitions/scrim-dress-...` green (from the
  bad-auto checkout; --state-dir is required on the operator);
  impact_service alias table covers apache/bind/mysql/exim (verified present
  in badauto/tactics/impact_service.py, 2026-09-20).
- Confirm both repos still clean/pushed at the end; list any straggler files.

## Phase 5 — report

Run `python3 scrim-report.py /home/hna/dev/dawgsec/scrim-runs/scrim-dress-...`
first — its INTERACTION.md numbers (interaction score, gates, red/blue sides)
belong in FINDINGS.md. Then write FINDINGS.md: what passed, what broke, what
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
