# Scrim harness upgrades — revision 2 (2026-09-21), implemented

Supersedes the original 2026-09-20 plan (the version that said "paste into a
fresh session"). This revision was verified against both repos and the 17c
evidence before implementation, corrected several of the original claims, and
has now been IMPLEMENTED. This doc is the map of where everything landed and
what remains (live validation only).

## Corrections the original plan got wrong

1. The sudo-nopasswd step is tezcatlipoca's `setup_ubuntu_auth`, not nakon's;
   for cloned teams it runs inside `clone_team_boxes`. D3 was a reorder there
   plus a guest-agent fallback in `fix_dns_on_boxes`, not a nakon change.
2. The Windows pivot needed no new credential tactic — `cred_spray` already
   attacks Windows over sshd. The path was dead because of three specific
   blockers (see A5 below for how each was removed).
3. `status_text` returns rendered text, so C1 needed a new parsed helper
   rather than "parse status_text".
4. Legacy run dirs have NO scoreboard series, no restore/inject records — half
   the report's metrics are "n/a (pre-scoreboard-state run)" by design; the
   honest red-side numbers carry the verdict there.
5. events.jsonl originally had no structured service/impact field; red now
   emits `data` on action events and `blue_restore`/`score` events.
6. Stall detection excludes health_check actions AND counts the opening
   (T0 → first successful action) — 17c's dead first 29 minutes was its
   biggest stall and the naive gap-between-successes definition missed it.

## Where things landed

### C — measuring stick (this repo)

- `run-agent-scrim.py`: `parsed_status()` (parsed per-team service state,
  shared by `status_text`/`team_down`), `monitor_loop` snapshots at T+0 then
  every 300 s to `run_dir/scoreboard-state.jsonl` (t_plus_sec, wallclock,
  per-team {service, up, error}); `stage_capture` freezes a copy in evidence/.
- `scrim-report.py`: writes `INTERACTION.md` per run dir — red
  takedowns/timeline/tactics/initial-access/targets/stalls/restore-reactions/
  Windows footholds, blue rc=0 cycles/restorations/down-minutes/injects/
  notebook liveliness/eradication, interaction score + FAILED-RUN verdict at
  score 0, gate table. `--self-test` pins the 17c numbers (4 takedowns, 3
  tactics, 1 re-kill, 1 inferred restoration, 0 injects, 2 stalls, score 2).
- `docs/rehearsal-gates.md`: the numeric gates; `scrim-report.py` `GATES`
  dict is the machine source. Tune after the first rehearsal, not before.

### Red aggression (both repos)

- `run-agent-scrim.py stage_red`: decision_window 3→2 min, down ramp 1/3/4→
  2/4/6 (min_standing_services floor stays 2), endgame at T-15, opening burst
  15 min, dead `pacing.profile` key dropped. bad-auto library defaults moved
  in parallel (window 2, quiet 1, ramp 2/4/5, `reimpact_after_min: 15`).
- bad-auto `prompts.py`: pressure rule fires below cap (not just at zero),
  "hold root + headroom → take it down this cycle", "if blue restores, break
  it again", OPENING SWEEP (foothold on every box, Windows included), and the
  restore-response doctrine with mechanism switching.

### A — red capability (bad-auto repo, commits 3d747e8 / 7669599 / 2af28e4)

- A1 restore-response: `Director._detect_restores` diffs consecutive
  scoreboard polls (DOWN→UP = blue restore) → `World.blue_events` +
  `blue_restore` log event + digest `blue_restores` block with a re-attack
  directive; second restore of the same service switches the directive/ladder
  to `impact_firewall`. `reimpact_after_min` (15) bounds impact exclusivity in
  the fallback.
- A2 `impact_firewall` tactic: on-box port resolution (MainPID → ss), iptables
  / ufw / nft insert chain, `mode="firewall"` impact (counts against
  `can_impact`), cleanup command in world notes, refuses port 22.
- A3 credlist sabotage: already existed (`credlist_sabotage`), gated by
  `credlist_gate_min`/`credlist_max_per_team`; organizer restore passwords in
  `<state>/report-secrets.md` (0600).
- A4 persistence: new `rekill` method — `/etc/cron.d/tznet-reaper` re-stops a
  downed service every 5 min (survives blue restores); persistence
  rate-limited to one artifact per foothold per hour.
- A5 Windows: fallback `cred_spray`/`foothold_ssh` picks no longer filter
  windows targets; `impact_service` takes the windows branch before the
  unit-regex/`can_root` gates; Administrator is admin-equivalent
  (`_can_admin`); `_WINDOWS_EFFECTS` maps the scored slugs (WinRM / SMB share
  / RDP misconfigs) to PowerShell effects with verification checks.
- A6 anti-stall: `World.tally_failure`/`failure_count` per (tactic, ip) fed
  from `execute()`; policy blocks a third impact attempt where two failed;
  digest exposes per-target `failed_attempts`.
- A7 adaptive tempo: blue restoring → 1.5-min windows for 20 min; scoreboard
  silent 30+ min → 4-min windows; persisted in `world.meta`, endgame wins.
- Also landed from the parallel session: pressure override below cap (incl.
  persistence as passive), structured `data` on action events, per-cycle
  `score` events, and `badauto/report.py` (end-of-run vulnerability report).

### B — blue effectiveness (this repo)

- B1: `stage_blues` writes `NOTEBOOK.md` (OPEN INCIDENTS / HUNT CHECKLIST /
  DONE); the cycle prompt embeds it + last 5 LOG.md lines; notebook update is
  cycle step 0. `stage_capture` archives it.
- B2: `scoreboard_delta()` renders CHANGES SINCE LAST CYCLE (new DOWN with
  RESTORE FIRST, back-UP with expect-re-attack, still-DOWN with since-T+) at
  the top of every cycle prompt.
- B3: `inject_brief()` queries /api/injects per cycle through the team jar;
  unsubmitted + due ≤30 min becomes TASK #1; submitted shows ✓.
- B4: opencode launch hardened — `stdin=DEVNULL`, `start_new_session=True`,
  per-team HOME/XDG under the run dir (state DB + server logs become
  evidence; `_opencode_log_tail` appends the server log on failure). The
  FIFO-supervisor fallback is NOT built — build it only if the hardened
  launch still dies in the live triage (10 consecutive rc=0 cycles is the
  gate).
- B5: cycle scope is "AT MOST TWO actions" (restore > inject > one hunt item);
  pacing measures actual cycle duration (sleep `clamp(30, 600 − took)`, first
  cycle starts immediately instead of after a 10-min sleep).

### D — deploy flakes (this repo)

- D1: `clone_ops.ensure_cloned_network` runs after the start loop: guest-agent
  IPv4 check per Linux box (30 s grace), repair on miss.
- D2: `clone_ops._repair_box_network` — live `ip addr/route` repair plus the
  persisted networkd `.network` (KeepConfiguration) + ifupdown stanza; the
  body is recorded in the function docstring and
  `docs/dress-rehearsal-prompt.md`'s playbook.
- D3: `setup_ubuntu_auth` now runs BEFORE `fix_dns_on_boxes` in
  `clone_team_boxes`, and `fix_dns_on_boxes` falls back to
  `guest_agent_exec_root` with `DNS_FIX_CMD_ROOT` (no sudo, no network) before
  giving up on a box.

## What still needs a live range

Nothing left in code. Remaining work is validation, in this order:

1. `python3 run-agent-scrim.py --competition scrim-dress-... --duration-min
   90 ...` per docs/dress-rehearsal-prompt.md — the rehearsal itself.
2. B4 live triage: blues must reach 10 consecutive rc=0 cycles on the local
   endpoint. If the hardened launch still dies, read the per-team opencode
   server logs (now under `blue-teamN/.opencode-home/`), then build the
   `--blue-worker N` FIFO supervisor only if the error is in-opencode.
3. Score the rehearsal with `scrim-report.py` → INTERACTION.md against
   `docs/rehearsal-gates.md`. Only a green INTERACTION.md is "ready for the
   practice". Retune gate numbers after the rehearsal, not before.
4. Watch one risk: 2-min decision windows triple red's LLM load on the single
   qwen endpoint shared with blues. If decision latency balloons, raise
   `decision_window_min` to 2.5 — don't drop actions.

## Guardrails (unchanged)

Node shared with the workshop portal (no workshop-* touches, hdd pool only);
`min_standing_services`=2 stays the floor and red never kills its own access
path (port 22 / sshd refused in impact_firewall, impact_service, and rekill);
firewall tamper and cred rotation count against the concurrent-down cap;
every red tactic leaves a blue-discoverable artifact and a cleanup note —
the point is practice, not undeletable damage.
