# Deploy prompt — scrim-one (exact redeploy, verified 2026-10-09)

Copy everything below the line into a fresh session. It redeploys **this same comp**
(`competitions/scrim-one`) from scratch and hands over a live, red-compromised range.

---

You are the operator for a tezcatlipoca range build. Work autonomously; report at each
gate. Do not ask questions unless a GO/NO-GO gate fails.

## Ground truth (verified 2026-10-09 — trust this over any older doc)

- Checkout: `/home/hna/dev/dawgsec/tezcatlipoca` (branch `main`). Python: **always**
  `.venv/bin/python` (system python3 is too old for vendored nakon).
- Node: **`proxmox` @ 10.0.0.150** (the shared Cyber Realm estate — 107 VMs live there, so
  never sweep anything you do not own; use `destroy-competition.py` from the comp dir).
  API endpoint `https://realm.hnasheralneam.dev:443`; that hostname resolves through
  Cloudflare and is **not SSHable** — use the LAN IP for `qm`/`pvesh`.
- Engine: **mgmt 10.0.0.252, gw 10.0.0.1, vmid 1000** (`.env` `TF_VAR_engine_mgmt_ip`).
  Never .250/.249 (tailscale-poisoned/contested).
- Team identifier **120** → subnet 192.168.120.0/24, boxes .2–.8, vmids 1400–1406.
- 7 boxes: dc01 (Windows), win02 (Windows), web01, mail01, db01, splunk01 (Linux) and
  **fw01 = in-path pfSense — infrastructure, never a target** (it is `unmanaged` +
  `in_path` in `boxes.json`, so deploy phase 5 bootstraps it automatically).
- Templates: box template `955 base-ubuntu24.04-fix`; goldens 1150–1155; engine template
  1140. Storage: **`hdd`** for the range and for red01 (`deploy.red_storage: hdd` — the
  default `hdrives-zfs` exists only on the other node).
- red01: **10.0.0.198** (`deploy.red_ip`), gw .1, vmid 999, `bad-auto` from the sibling
  checkout `~/dev/dawgsec/bad-auto` (git remote `DawgSecTeam/tlaloc`).
- Compfile knobs already set: `assume_breach 1`, `assume_breach_depth 3` (all persistence
  families on Linux + Windows, every beacon transport), `remote_access 0` (do not consume
  the shared headscale slots for a verification run), `firewall_dnat 4470->10.0.0.198`.
- Secrets live in `competitions/scrim-one/.deploy_state.json` + `credentials.txt` (both
  gitignored) — never print their values, never commit them.

## Step 0 — preflight (do not skip; each has bitten a real run)

```bash
cd ~/dev/dawgsec/tezcatlipoca
git status -sb                      # must NOT show 'HEAD (no branch)' / 'UU' — a parked
                                    # pull --rebase silently strips uncommitted work.
                                    # Recover: git rebase --abort, then ONE merge.
git log --oneline -1                # confirm your feature commits are on disk
python3 -m unittest discover -s tests 2>&1 | tail -3
#   expected: Ran 1309 tests ... FAILED (failures=1, errors=3, skipped=17) — the 4
#   test_multinode/test_run_ownership failures are upstream-broken, not yours.
scripts/orphan-sweep.sh 8180 4470   # stray reverse tunnels / beacon listeners
```

## Step 1 — tear down whatever is live (own it first)

```bash
.venv/bin/python destroy-competition.py --competition scrim-one --yes --full --end-of-competition
# then, if red01 survived (its destroy is separate from terraform):
.venv/bin/python -c "import sys;sys.path.insert(0,'.');import red_plant_ops as rp;print(rp.destroy_red('competitions/scrim-one').stdout[-400:])"
```

Verify gone: no vmids 999/1000/1140/1150–1155/1400–1406 in `proxmox-cli list-vms`, and
`vmbr120`/`vmbrW120` absent from `pvesh get /nodes/proxmox/network`.

## Step 2 — deploy (background; ~95 min on this node)

```bash
.venv/bin/python -u create-competition.py --competition scrim-one \
  --teams 1 --scoring-vmid 1000 --yes > logs/e2e-scrim-one-$(date +%Y%m%d-%H%M).log 2>&1 &
```

Run it as a tracked background process and wait for its exit notification. Success =
the final summary block (scoreboard URL, team/box creds, "scrim-one is live") and
`DEPLOY_EXIT=0`. Expect the slow steps to dominate: Windows bootstrap ~80 min, DOMAINS
~65 min.

**A deploy can finish "successfully" with red missing — always check the tail for
`[!] tolerated failure(s)` and read `degradations` in `competitions/scrim-one/.deploy_state.json`.**

## Step 3 — infrastructure gate (must be PASS)

```bash
.venv/bin/python -u verify-competition.py competitions/scrim-one \
  --strict-services --fix-round-loop > logs/verify-e2e.log 2>&1
```

Required: `RESULT: PASS — competition looks healthy` (services UP, 12 pins registered,
firewall in-path, misconfigs, 12 injects, round loop **advancing**, all 6 machines planted,
domains validated). `plant integrity` may warn about 1 failed nakon step
(`ldap-signing-off-win` on dc01) — known catalog gap, not a blocker.

## Step 4 — red presence (the part that silently fails)

The deploy's phase-7 hook (`red_plant_ops.plant_assume_breach`) deploys red01, builds the
Realm C2 on it (tavern + all callback transports + MCP at `/mcp`), stages the imix implants
and seeds depth 3. Confirm in the deploy log:

```
assume-breach: deploying red01 + realm engine DNAT (bad-auto)...
realm-c2: tavern live on red01 10.0.0.198 (transports: grpc, http1, dns, icmp, quic; MCP at /mcp)
assume-breach: seeded (depth 3) — every box is owned before T0.
```

If any of those lines is missing or says `No route to host`, **stop and check red01's actual
address before anything else**: `proxmox-cli vm-info 999 | grep ipconfig0` must read
`ip=10.0.0.198/24`. A mismatch means badauto silently fell back to its built-in defaults (a
relative/absent `--config` does this) — every red step then targets .198 while red01 is
elsewhere. The operator-side fixes are committed (`realm_c2_ops.write_local_c2_config`
resolves, `red_plant_ops._deploy_red` refuses a missing config), so a recurrence means
something re-introduced a relative path — fix it, do not hand-patch the VM.

Also confirm the key reached the beacons (`realm-c2: could not read tavern's public key` is a
red flag): if it appears, push the key and re-plant —

```bash
.venv/bin/python -c "import sys;sys.path.insert(0,'.');import realm_c2_ops as rc;pk=rc.tavern_pubkey('10.0.0.198','$PWD/proxmox');print(pk, rc.set_realm_pubkey('$PWD/proxmox','10.0.0.198',pk).returncode)"
# then clear meta.seed[ip].implant for the 6 boxes in /var/lib/bad-auto/world.json and:
ssh -i proxmox sysadmin@10.0.0.198 'cd /opt/bad-auto && sudo -n python3 -m badauto seed --competition /var/lib/bad-auto/intel --state-dir /var/lib/bad-auto --intel nakon --depth 1'
```

## Step 5 — red team bring-up (what "ready for a human blue team" means)

1. **LLM path.** `bad-auto.service`'s config points at `http://localhost:8180/v1` — supply the
   tunnel the harness normally does, from this host:
   `ssh -N -R 8180:100.64.0.19:8000 -o ExitOnForwardFailure=yes -i proxmox sysadmin@10.0.0.198`
   (GX10 serves `qwen3.8-flash-next`.) Verify from red01: `curl localhost:8180/v1/models` → 200.
2. **Timeout.** `llm.timeout` must be 300 (and `max_tokens` 6144): a reasoning model needs
   >2 min per call, and at 120 every decision falls back silently — red looks busy and thinks
   nothing. Proof to demand: `{"kind":"llm","ok":true}` **and** a `cycle` with
   `"source":"llm"` followed by an `action` with `"source":"llm"`.
3. **Start it:** `ssh -i proxmox sysadmin@10.0.0.198 'sudo -n systemctl enable --now bad-auto'`
   (SIGTERM is slow; a restart takes ~90 s and systemd SIGKILLs it — expected).

## Step 6 — verification (report these numbers, not adjectives)

- **Beacons, per transport × OS, freshness-gated** (a tavern row survives its unit, so sample
  twice and require `lastSeenAt` to move): all five of
  `grpc, http1, dns, icmp, quic` on Linux **and** Windows, `0 lost` in `beacon_score`.
- **Persistence:** census red01's `world.json` artifacts by kind — expect every family
  (Linux: systemd, cron, authorized-key, sshd-dropin, suid, profile, rogue-user, decoy,
  evasion:*; Windows: service, schedtask, runkey, wmisub, defrun, decoy, evasion:*; plus the
  rawsock tier) and `meta.seed_done` non-null.
- **Red acts:** at least one LLM-sourced action against a box, and the scoreboard reflecting it
  (`score … services_down`).
- Grade the red-side numbers against `docs/rehearsal-gates.md`.

## Step 7 — hand-off

Leave the range up and tell the user: scoreboard URL, the red state (`bad-auto` active, N/N
beacons live, last LLM action), and how to start a fresh play — `systemctl restart bad-auto`
on red01 (lines red's clock up with the play) then
`verify-competition.py competitions/scrim-one --strict-services --fix-round-loop` to re-arm
the scoreboard loop. Teardown when done:
`destroy-competition.py --competition scrim-one --yes --full --end-of-competition`.

## Traps that cost real time (do not rediscover these)

- **Every path handed to bad-auto must be absolute** (`cwd=bad-auto`): `--competition`,
  `--config`, `BAuto_COMPETITION_DIR`. A relative path fails silently — worst case as built-in
  defaults (`red_ip 10.0.0.199`).
- **bad-auto runs on red01 from `/opt/bad-auto`, a SYNCED COPY (no `.git`).** Editing this
  checkout changes nothing for a live range; copy the file over and prove it landed.
- **`badauto seed` skips stages already recorded done** (`world.json` → `meta.seed[ip]`):
  a re-seed reports `boxes: 7` and plants nothing. Clear the flag you want re-run.
- **Pause the Hourly Orchestrator cron job** (`2398d312c7b3`, script
  `~/.hermes-qa/hourly-orchestrator.sh`) before a deploy and resume it after: its
  `git pull --rebase` in this checkout parked a conflicted rebase twice and silently stripped
  uncommitted work both times.
- **Windows beacons must be launched from a scheduled task, never `sc create`** — imix's
  default features exclude `win_service`, so the SCM kills the process after its single
  check-in.
