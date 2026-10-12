# Handoff — automated Realm C2 deployment (2026-10-11)

Paste everything below the line into the agent that will continue this work.

---

You are finishing the setup and testing of the **automated Realm C2 deployment
pipeline** on the DawgSec CCDC training estate: tezcatlipoca (range builder) +
bad-auto (autonomous red teamer) + Realm C2 (tavern + imix beacons) on red01.
All code-side work is **done and pushed**; what remains is a clean **live
deploy of the scrim-one competition from scratch, plus full end-to-end
validation of the automated red/C2 stack**.

This is sanctioned CCDC training on club-owned kit (dawgsec/cyberdawgs).

## Read FIRST (you have no other memory — these are your skills)

1. `~/dev/dawgsec/tezcatlipoca/competitions/scrim-one/deploy-prompt.md` — the
   verified step-by-step redeploy runbook (ground truth, phases 0–7, trap
   list). **Follow it step by step.**
2. `~/dev/dawgsec/tezcatlipoca/AGENTS.md` + `docs/rehearsal-gates.md`
3. `~/dev/dawgsec/bad-auto/AGENTS.md` + `README.md`
4. `~/dev/dawgsec/tezcatlipoca/references/realm-c2-cold-provision.md` — what
   to do when the C2 phase "succeeds" but the red C2 is not usable.

## Ground truth (verified 2026-10-11 — trust this over anything older)

- **The estate is fully torn down** (verified on the node): no vmids
  999/1000/1140/1150–1155/1400–1406, no `vmbr120`/`vmbrW120` bridges. You
  start clean; the runbook's Step 1 teardown should find nothing but still run it.
- **HEADs, both verified in sync with origin:**
  - `~/dev/dawgsec/tezcatlipoca` → `07eab74` (remote `git@github.com:DawgSecTeam/tezcatlipoca.git`)
  - `~/dev/dawgsec/bad-auto` → `e4fca4f` (remote `git@github.com:DawgSecTeam/tlaloc.git`)
  - One dirty file, intentional: `bad-auto/destroy-failure.log` (tracked
    rolling runtime log). Leave it uncommitted.
- Python: **always** `.venv/bin/python` in tezcatlipoca; **`/usr/bin/python3`**
  for bad-auto (the agent-venv python lacks its deps).
- bad-auto tests: run `python3 -m pytest tests/ -q` — **never**
  `unittest discover` (it silently skips pytest-only files). Baseline at
  `e4fca4f`: 513 passed / 7 skipped / 0 failed.
- tezcatlipoca tests: `python3 -m unittest discover -s tests` — 4 known
  upstream-broken failures (`test_multinode`/`test_run_ownership`); establish
  the full baseline before you deploy and compare after your changes.

## Fixes landed SINCE the runbook was verified (runbook verified @ `8f598f6`, 2026-10-09)

These are on top of the runbook — you should not need to re-fix them, but know
they exist:

- `5b1e398` + `774161d` — realm-c2 provisioner now **hard-gates on tavern
  binding AND answering `:8000`** and retries the pubkey read: the
  first-provision secrets race (tavern self-generates
  `~/realm/secrets/tavern-secrets` on first start, then never binds) is fixed;
  a fresh deploy self-heals.
- `fa85386` — the scrim harness gives the local red LLM endpoint headroom
  (`timeout 300 / max_tokens 6144`); hand-run deploys still read the
  gitignored `~/dev/dawgsec/bad-auto/config.yaml` — set those values there.
- `6e27140` — teardown pulls red's final state BEFORE the report grades it.
- `07eab74` — round-loop treats paused-after-reboot as stale; identifier
  defaults come from packet history.
- bad-auto `c79d14b` — `realm.pubkey` stays in the red01 config push allowlist
  (the ConfigContractError regression is fixed) and `impact_firewall`
  whitelists the unit stem before any root-shell interpolation.
- bad-auto `f7020f0` — the director spreads impacts across boxes instead of
  re-breaking one.

## Definition of DONE (the "testing" half — report numbers, not adjectives)

1. Deploy exits 0 with **no red degradations**: log contains
   `realm-c2: tavern live on red01 10.0.0.198 (transports: grpc, http1, dns, icmp, quic; MCP at /mcp)`
   and `assume-breach: seeded (depth 3)`.
2. `verify-competition.py competitions/scrim-one --strict-services --fix-round-loop`
   → `RESULT: PASS`, round loop **advancing**.
3. **Beacons, freshness-gated**: all five transports on Linux AND Windows for
   every box. Sample tavern's MySQL twice ~50 s apart and require
   `last_seen_at` to MOVE (a row outlives its unit). Expected shape for this
   comp: 6 hosts × {GRPC 2, HTTP1 1, DNS 1, ICMP 1, QUIC 1} = 36 rows.
   `beacon_score` reports `0 lost`.
4. **Persistence census** from red01 `/var/lib/bad-auto/world.json`: every
   artifact family per runbook Step 6, `meta.seed_done` non-null.
5. **Red acts via the LLM path**: `events.jsonl` shows
   `{"kind":"llm","ok":true}` and a `cycle`/`action` with `"source":"llm"`;
   at least one LLM-sourced action visible on the scoreboard.
6. Grade against `docs/rehearsal-gates.md`; **leave the range up**; report the
   scoreboard URL + red state (`bad-auto` active, N/N beacons live, last LLM
   action) and how to start a fresh play.

## Environment facts you need

- You run on the club workstation as user `hna` (Fedora). Everything is local.
- Proxmox node `proxmox` @ 10.0.0.150 — SSH key `~/dev/dawgsec/tezcatlipoca/proxmox`
  (`root@10.0.0.150` for `qm`/`pvesh`; `sysadmin@` for red01). The shared
  estate has ~107 other VMs — **never sweep or destroy anything you do not
  own**; use the comp-dir tools.
- **Before the deploy: pause the Hermes Hourly Orchestrator cron** (id
  `2398d312c7b3`, script `~/.hermes-qa/hourly-orchestrator.sh`) — its
  `git pull --rebase` has twice parked this checkout in a conflicted rebase
  and silently stripped uncommitted work. Run `hermes cron pause 2398d312c7b3`
  (CLI at `~/.local/bin/hermes`), and `hermes cron resume 2398d312c7b3` after
  the deploy finishes. `git status -sb` must never show `HEAD (no branch)`/`UU`.
- The deploy is ~95 min (Windows bootstrap ~80, DOMAINS ~65 dominate). Run it
  as a background process you poll; do not foreground-timeout it.
- Secrets live in `competitions/scrim-one/.deploy_state.json` +
  `credentials.txt` (both gitignored): **never print their values, never
  commit them.** Same for `report-secrets.md` (0600 on red01).

## Traps that cost real time (do not rediscover them)

- Every path handed to bad-auto must be **absolute** (`cwd=bad-auto`).
- bad-auto runs on red01 from `/opt/bad-auto`, a **synced copy (no `.git`)** —
  editing your local checkout changes nothing live; copy files over and prove
  they landed. Operator-side `python3 -m badauto …` must run with
  `cd /opt/bad-auto` first.
- `badauto seed` skips stages already recorded done in `world.json`
  (`meta.seed[ip]`) — clear the specific flag you want re-run.
- red must **never** target fw01 (in-path pfSense, `unmanaged` — it carries
  all scoring traffic). Fix the comp declaration, never hand-edit `world.json`.
- Windows beacons are planted by `realm_plant_win` from a scheduled task,
  never `sc create` (imix's default features exclude `win_service`; the SCM
  kills it after one check-in).
- A beacon that never registers is usually the **server pubkey**
  (`IMIX_SERVER_PUBKEY`), the **first-provision secrets race** (fixed; re-run
  just the provisioner `realm_c2_ops.prepare_local_c2` → `provision_realm_c2`
  after a degradation), or a **stale DNAT rule** — prove with
  `~/dev/dawgsec/bad-auto`'s realm smoke tooling / tavern's journal before
  rebuilding anything.
- When something fails, work from evidence (tavern journal, box
  `journalctl`, `world.json`, `events.jsonl`); fix real bugs in the source
  checkout, test, commit, and push. Do not fabricate a passing result — an
  honest blocker report is the correct outcome if you hit one.
