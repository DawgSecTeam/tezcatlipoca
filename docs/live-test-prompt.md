# Live test prompt — tlaloc in a real range

Paste the block below into a fresh session that has shell access to the range host. It
tests the red team the way a competition uses it: a deploy, a running agent, a captured
verdict, and a teardown. It is deliberately evidence-first — every step names what to
capture and what makes the step FAIL.

Estate values are not in this file. The session must read `infrastructure-status.md`
(gitignored, local only) for hosts, tokens, template vmids and the live-ownership map.

---

You are testing **tlaloc**, the red-team component of the tezcatlipoca scoring range, on
a live estate. Your job is to prove the whole red lifecycle end to end and to report,
with evidence, exactly where it breaks. A failure reported honestly is a good result; a
plausible summary of a test you did not run is a failed test.

## Rules

1. **Read first**: `AGENTS.md`, `infrastructure-status.md`, `docs/architecture.md`,
   `docs/usage-agents.md`, `docs/scrim-harness.md`, `tlaloc/README.md`,
   `tlaloc/AGENTS.md`, `tlaloc/docs/deploy.md`, `tlaloc/docs/testing.md`.
2. **Never test from the main tree.** Submit the run to the dashboard (the central run
   queue, address/token in `infrastructure-status.md`, MCP at `/mcp`) or cut a NEW
   worktree off `main` and deploy from there. The main tree is for reading.
3. **No workarounds.** No hand-editing a VM, no `-force`, no manually clearing a flag or
   a lock, no `git` surgery mid-run. If something needs a workaround, that is the finding
   — stop, capture the evidence, and report it.
4. **Capture, don't summarize.** For every gate below, record the command and its real
   output (or the path of the log/artifact holding it). Timestamps in EST/EDT.
5. **Tear down what you deployed**, with `destroy-competition.py` unless the run is meant
   to be kept. A practice range that keeps templates uses `--teams-only` (or a scrim with
   `--keep-templates`).

## What you need before starting

- A competition whose Compfile is a tlaloc smoke range. `tlaloc-smoke` in
  `tezcatlipoca-competitions` is the intended one; read its Compfile and confirm:
  `red.assume_breach light` (or full), `red01_vmid` / `red01_ip` (or a free slot), a C2
  callback port (`firewall_dnat <port>-><ip>` or `red.c2_callback_port`), and (for the
  full test) `red.clef_url` / an LLM endpoint.
- A free red01 vmid/IP for this run. The preflight refuses an occupied slot and names
  the owner — if it refuses, pick another Compfile, do not override the guard.
- A free scoring vmid and a team-identifier block clear of other live ranges (the
  preflight/locks refuse real collisions).

## The test

### 1. Preflight and identity

- Submit the deploy (dashboard preferred) or run
  `create-competition.py --comp-dir <comp> --scoring-vmid <free> --plan-only`, then the
  real deploy with `--teams N --yes` from the worktree root.
- PASS: plan-only exits 0 and names the boxes; the real deploy prints a red01 vmid/IP
  that matches the competition's reservation, not another range's.

### 2. Phase 7 — red is provisioned and owned

Capture, from the deploy log and the Proxmox API/UI:

- red01 exists at the reserved vmid/IP as a **linked clone** of `base-ubuntu24.04-fix`,
  carries the run's full tag set (`tezcatlipoca;comp-<name>;run-<id>`), `onboot`, and
  two NICs (management + a `vmbrR<hex>` red bridge).
- Inside red01 (guest agent or SSH): `ip -4 addr` shows the management address and
  `10.200.0.10/24`; `ip route` sends `192.168.0.0/16` via `10.200.0.1`; cloud-init
  networking is disabled (`/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg`).
- Disk grew to the configured floor (default 60G) as seen by `df -h /` on the box.
- `/opt/tlaloc` contains the package and `systemd/tlaloc.service` exists; the unit is
  enabled.
- The pushed config exists at `/etc/tlaloc/config.yaml` (0600) and the deploy's contract
  check passed (no "dropped field" failure).
- The seed manifest was pulled to `<comp>/red/manifest.json` and lists at least one box
  with a landed stage. FAIL if the deploy claimed success without it.
- On the engine: `systemctl is-active range-red-firewall.timer` is `active`;
  `iptables -S FORWARD` shows the red→team ACCEPT and the DROP toward the red segment;
  `iptables -t nat -S PREROUTING` shows the callback port DNAT to `10.200.0.10:<port>`.
- C2 (unless `red.c2 off`): Mythic answers, the competition's operation exists, and
  `competitions/<id>/red/payloads/manifest.json` maps payloads to boxes.

FAIL if any item is missing while the deploy printed success. That is the class of bug
this test exists to find.

### 3. Red can actually act

- Start/observe the agent (`tlaloc.service`). Watch `/var/lib/tlaloc/events.jsonl`:
  decisions with their source (`clef`, `ladder`, `deterministic`), then `action` rows.
- `verify-competition.py --competition <id> --red-identity --red-teams all`:
  `red_identity` and `red_teams` must both PASS (red's source survives to the boxes and
  it can dial every team).
- If an LLM endpoint is available, confirm at least one decision came from `clef` and
  cite the bundle/decision rows. If no endpoint is reachable, the deterministic ladder
  must still act — a run where red does nothing is a FAIL either way.
- PASS: at least one real action against a scored box, and the scoreboard/blue sees it.
  Record the box, the service and the observable effect. **A red-vs-empty-room run is a
  failed run**, no matter how healthy red's own log looks.

### 4. Verdict and teardown

- Run the standard teardown (`destroy-competition.py … --yes`; or the dashboard's
  destroy run). Before it deletes the VM it must:
  - run `tlaloc report` on red01 and pull `/var/lib/tlaloc/` into `<comp>/red/`:
    `report.md`, `restore-table.json`, `report-secrets.md`, `world.json`,
    `events.jsonl`, plus the manifest and `verdict.json`;
  - copy box logs into the run's test folder (`evidence/box-logs/`);
  - fill `RED-TEAM.md` from `report.md`.
- Teardown must exit 0 only if red01 is gone: confirm the vmid is absent, the engine's
  `range-red-firewall.timer`/rules are removed, and the teardown's own proof names the
  vmid it checked.
- FAIL if any of the verdict files are missing, if the teardown reports clean with red01
  (or another run's template) still present, or if a second run's VM was touched.

### 5. Repeatability

- If time allows, destroy and deploy the same competition again: templates should be
  reused by their stamp (not rebuilt), red01 should get a **new** run id and tags, and
  golden plant checkpoints from the earlier run must not skip this run's plants. The
  deploy's first-log line names the reused template.
- `--replant-goldens` must force a full golden re-plant; verify from the log that no
  checkpoint was honored.

## Verdict

Report, in this shape:

```
VERDICT: pass | pass-with-findings | fail
ENVIRONMENT: comp / run id / engine node / proxmox node / worktree or dashboard run
GATES: preflight=<PASS/FAIL> phase7=<…> red-actions=<…> verdict=<…> teardown=<…> repeat=<…>
EVIDENCE: <command → output path or quoted result> per gate
FINDINGS: <severity · file:line where in this repo · what broke · reproduction>
CLEANUP: <what was destroyed, what was kept, the teardown's exit code and proof>
```

Be specific and be harsh. The point is to find the thing that embarrasses the club
during an event.
