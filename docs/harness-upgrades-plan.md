# Scrim harness upgrades — plan for a new session

Paste everything below the line into a fresh agent session. Work through the
workstreams in order (C is small but do it FIRST — it is the measuring stick
for everything else). Everything lands in the two repos on main and gets
pushed; nothing here needs a live range until the verification step.

---

## Context

The scrim system simulates a CCDC-style competition: tezcatlipoca
(/home/hna/dev/dawgsec/tezcatlipoca) deploys and runs the range; bad-auto
(/home/hna/dev/dawgsec/bad-auto) is the red agent. The 2026-09-18 round-3 run
(evidence: /home/hna/dev/dawgsec/scrim-runs/agent-scrim-2026-09-17c/, debrief:
that dir's FINDINGS.md) proved red's machinery but exposed that **the
competition itself is not real yet**:

- Red: 26 attack actions, all one pattern (cred_spray → privesc → stop/mask),
  3 of 16 scored services touched, 35 minutes lost to one repeated mistake,
  and it re-attacked a blue restore exactly once, by accident.
- Blue: 40/40 orchestrator cycles died instantly (opencode background bug —
  retry logic has since landed but is NOT yet validated live), 2 manual cycles
  did good detection but zero remediation, zero injects, empty notebooks.
- Interaction: red took ~26 actions; blue affected the game once. That is the
  number this plan exists to change.

Already fixed and validated on 2026-09-19/20 (do NOT redo): shared Quotient
cookie jars, team-ID mapping in the scoreboard path, engine compose timeout,
opencode cycle retry + 60k local ctx, keyless local LLM endpoints, bad-auto
red01 stale-state archive, on-box systemd unit resolution (apache→apache2).
Local LLM endpoint for both teams: http://100.64.0.9:8080/v1 (qwen3.6-35b-a3b;
blues reach it directly, red01 needs a LAN address or a relay+tunnel — see
docs/dress-rehearsal-prompt.md Phase 3).

## Workstream C (do first) — interaction metrics, so progress is measurable

Build `scrim-report.py` in the tezcatlipoca repo. Input: a run dir. Output:
`INTERACTION.md` next to the run's FINDINGS.md, containing:

- Red: takedown count + timeline, distinct tactics used, distinct initial-
  access techniques, targets touched, stall periods (≥10 min without a
  successful new action), restore-reactions (a service blue brought back that
  red attacked again — detect via events.jsonl impacts on the same
  target/service after an UP transition).
- Blue: successful cycle count (feed.log rc=0), restorations performed and
  time-to-restore (needs monitor data — see below), injects submitted, eradication
  evidence (LOG.md/submissions mentions of planted artifacts being removed),
  notebook liveliness (LOG.md entry count).
- Availability damage: per-team service-down minutes. Source of truth: the
  orchestrator should snapshot parsed scoreboard state (status_text →
  services + UP/DOWN) to `run_dir/scoreboard-state.jsonl` on every monitor
  pass — ADD THIS WRITE to monitor_loop. The report derives down-windows from it.
- An interaction score with explicit components: restorations performed,
  red restore-reactions, evictions, injects, eradication — each a count.
  A run where this score is 0 is a failed run NO MATTER how good red's log
  looks. Print that verdict line.

Also write `docs/rehearsal-gates.md` (or extend docs/dress-rehearsal-prompt.md
Phase 3) with the numeric gates a rehearsal must hit, e.g.: red ≥6 takedowns
AND ≥3 restore-reactions AND ≥4 distinct tactics AND ≥1 pivot to a Windows
box AND zero ≥10-min stalls; blue ≥8 rc=0 cycles AND ≥2 restorations AND
≥2 injects AND LOG.md ≥10 entries. Tune the numbers after the first rehearsal,
not before.

## Workstream A — red aggression (bad-auto repo)

Design principle: red's job is a sustained tug-of-war, not a single sweep.
Every item keeps the existing guardrails (min_standing_services, don't kill
red's own SSH path, endgame gates).

A1. **Restore-response loop.** bad-auto already polls the scoreboard via
    QuotientSensor (badauto/quotient.py, admin session, per-team
    /api/services). Extend it to diff consecutive polls: a service
    transitioning DOWN→UP is a blue restore event. Persist it to world
    (e.g. world.blue_events) and emit it into the director's digest as a
    high-priority objective: re-attack that target within the pacing window.
    Escalation ladder on re-attack — first re-kill (stop_mask), then if blue
    restores AGAIN: switch mechanism (see A2/A3), never repeat the identical
    action more than twice.

A2. **New tactic: firewall tamper** (`impact_firewall`, new file in
    badauto/tactics/, pattern-copy impact_service.py): with root, insert a
    rule dropping the scored check port (e.g. `iptables -I INPUT -p tcp
    --dport <port> -j REJECT` plus equivalents for ufw/firewalld). Service
    stays "up", check fails — the most CCDC-authentic failure mode and the
    one blue practice most needs. Must report which port/service it poisons
    in the tactic result and world.note, and include a matching self-cleanup
    note (blue-visible artifacts are the point). respects min_standing_services.

A3. **New tactic: service-account credential rotation** (this is the already-
    planned "credlist sabotage", gated by the existing credlist_gate_min /
    credlist_max_per_team pacing knobs — implement the tactic those knobs were
    waiting for): rotate the password of ONE credlist check user on one box
    (the linux.credlist users the SSH/SMTP/IMAP/SQL checks authenticate as).
    Availability check starts failing auth while the daemon runs. ONE user,
    ONE box per gating window — full credlist destruction stays an endgame move.

A4. **Live persistence planting** (`plant_persistence`): per foothold, during
    the event (not pre-planted), one of: UID-0 user, /etc/cron.d re-kill job
    (cron that re-stops a previously-downed service every N min — direct
    counter to blue restores, self-healing pressure), or a systemd unit.
    Rate-limit one artifact per foothold per hour; log to world so the report
    can count eradication targets. This is what makes blue's hunt meaningful —
    every artifact red plants is something blue can find and remove for credit.

A5. **Windows pivot.** Red never touched dc01/win01, which hold the richest
    scenario material. Add a winrm/smb credential attack tactic (the transport
    already supports windows=True; impact_service._windows shows the pattern;
    the box password is in intel/credentials the same way the linux shared
    cred was). Goal: foothold on at least one Windows box per team mid-event,
    enabling the Windows impact path (WinRM stop/disable) and making blue
    defend the DC — the actual CCDC heart.

A6. **Anti-stall director rule.** Generalize the round-3 unit-name lesson:
    after 2 failed attempts of the same tactic on the same target, the
    director must switch target or tactic (tracking in director.py; the
    on-box unit resolution already removed the biggest cause). Also feed the
    digest the resolved unit names A1 learns so the LLM stops re-guessing.

A7. **Adaptive tempo.** Pacing knobs already model deadline/endgame; add a
    blue-competence signal: if restores happen within 10 min of takedowns,
    tighten decision_window_min (toward the burst profile); if blue is silent
    for 30+ min, ease off (real red reconnoiters before pressing a silent
    network). Keep this simple — two regime switches, not a controller.

Acceptance for A (measured by scrim-report.py on a live run): ≥4 distinct
tactics, ≥3 restore-reactions against a restoring blue, ≥1 Windows foothold,
zero ≥10-min stalls, and blue's notebook/logs show artifacts red planted
getting hunted.

## Workstream B — blue effectiveness (tezcatlipoca repo)

B1. **Persistent shared notebook.** Promote LOG.md from write-only diary to
    the team's working memory:
    - stage_blues writes a structured `NOTEBOOK.md` per team: sections
      "OPEN INCIDENTS", "HUNT CHECKLIST" (generic CCDC hunt list: rogue
      users/UID-0, cron, systemd units, sudoers, firewall, listeners,
      Windows services/tasks/run-keys, scheduled tasks), "DONE".
    - blue_cycle_prompt embeds the notebook's current content (it's small)
      plus the last cycle's final 5 lines, and instructs: update the notebook
      FIRST (move found items, add new incidents), then act.
B2. **Scoreboard deltas in every cycle prompt.** monitor_loop (or each
    blue_feed_loop iteration) diffs the parsed scoreboard against the
    previous snapshot and renders "CHANGES SINCE LAST CYCLE: web01-http DOWN
    (since T+63), db01-sql restored at T+71" at the top of the prompt, with
    the down-service restoration as standing task #1. This single change is
    what turns parallel monologues into a game: blue sees red's moves within
    minutes.
B3. **Injects that actually happen.** Cycle task ordering: if an inject is
    due within 30 minutes and not yet submitted (query via the shared jar),
    it becomes task #1 with the submit command inline. Track submissions per
    team in the orchestrator (parse /api/injects once per cycle) so the
    prompt can say "submitted ✓" instead of guessing.
B4. **Live validation of the opencode launch path (do this FIRST in B).**
    The retry landed but 40/40 still died in round 3. Before anything else:
    reproduce the exact feed-loop invocation (python subprocess from a
    thread, capture_output) against the local endpoint with a scratch
    workdir. If it survives → good. If it dies → implement the fallback now,
    while there's time: `run-agent-scrim.py --blue-worker N` child process
    started via a foreground-owned supervisor, fed cycle requests over a
    FIFO; the feed loop posts requests instead of spawning opencode. (The
    pty hypothesis was already tested and the bug did not reproduce — but
    "did not reproduce" is not "works"; round 3 taught us that.)
B5. **Cycle length for local qwen.** Local turns take 30–60 s and qwen
    thinks slowly; 5-minute tasks get cut mid-remediation (round 3's manual
    cycles died hunting). Make the cycle task scope realistic: max 2 actions
    per cycle (one incident OR one hunt item OR one inject), and pace cycle
    frequency off actual cycle duration rather than fixed sleep math.

Acceptance for B (live run): ≥8 rc=0 cycles per team, LOG.md/NOTEBOOK.md
growing every cycle, ≥2 restorations with time-to-restore under 15 min,
≥2 injects submitted, and the delta line visible in every prompt file in
cycles/*.prompt.txt.

## Workstream D — deploy-flake automation (small, do while deploys simmer)

D1. In clone_ops/deploy: after team2 linux clones start, guest-agent check
    for a routable IPv4; if missing, apply the known repair (ip addr + route +
    persistent config) instead of failing phase 6/7 an hour later.
D2. For Debian-based clones (app01), write the networkd .network file with
    KeepConfiguration at clone-prep time (the ifupdown carrier-blip lesson
    from e2e-2026-09-19).
D3. Move the DNS fix (hardening_ops) to after nakon's sudo-nopasswd step for
    cloned teams, or switch it to the guest-agent root path — it currently
    soft-fails 8× per fresh clone because sudo needs a password.

## Order and verification

1. C (report + gates) — half a day, no range needed; test against the 17c
   evidence in scrim-runs/agent-scrim-2026-09-17c/ (it must score that run's
   interaction honestly: ~1 restoration, 0 re-kills, 0 injects).
2. B4 (opencode liveness) — one hour, needs only the local LLM endpoint.
3. A1–A7 and B1–B5 in parallel tracks; each tactic lands with a
   `badauto run --once --dry-run` green and a unit-pattern-consistent file.
4. D while a deploy runs.
5. Final: run the dress rehearsal (docs/dress-rehearsal-prompt.md) with the
   NEW gates from C, produce INTERACTION.md, and only a green INTERACTION.md
   counts as "ready for the practice".

Guardrails: node shared with the workshop portal (no workshop-* touches, hdd
pool only); red keeps min_standing_services and never bricks blue's access
path; new red tactics must be blue-discoverable (leave the artifact, note it
in world) — the point is practice, not undeletable damage.
