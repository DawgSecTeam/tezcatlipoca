# ROE: bounds as data

Every bound is a config/Compfile value. Nothing about the rules of engagement is
hardcoded in a module — the earlier agent shipped a 420-minute clock inside
`escalation.py` that misfired on every shorter event, and three separate lists of
"unrecoverable" modes that drifted apart.

## Where the values come from

```
DEFAULTS (config.py)  <  config.yaml  <  env allow-map (TLALOC_*)
                      <  Compfile red.* block   (the ROE authority)
```

The Compfile is `key value` lines (tezcatlipoca's format), with a `red_yaml`
pointer for a nested block:

```
red.assume_breach full
red.depth 3
red.duration_min 150
red.caps.max_down 4
red_yaml red.yaml          # optional: ladder/windows/budget as YAML
```

`validate_config()` runs at load and at every Compfile merge: a bad ROE is a
deploy-time error, never a runtime surprise.

## The block

```yaml
red:
  assume_breach: full     # full | light | off  (seeding stage, see docs/deploy.md)
  depth: 3                # 0 access · 1 +escalate · 2 +channel · 3 +persistence/evasion
  duration_min: 150       # THE event clock; every time is a pct of it
  ladder:
    enabled: true
    pause_after_restore_min: 10      # the packet's "give them a break"
    tiers:
      - {name: observe, at_pct: 0.00, modes: [recon],              boxes_per_team: 0}
      - {name: easy,    at_pct: 0.15, modes: [stop],               boxes_per_team: 1}
      - {name: medium,  at_pct: 0.55, modes: [stop, stop_mask],    boxes_per_team: 2}
      - {name: hard,    at_pct: 0.72, modes: [stop, stop_mask, block_port], boxes_per_team: 2}
      - {name: brutal,  at_pct: 0.86, modes: [stop, stop_mask, block_port, stop_rename_config],
        boxes_per_team: 4, unrecoverable: true}
  caps:    {max_down: 5, min_standing: 1, grace_min: 10}
  windows: {endgame_teardown: 15, lockout: 3}
  budget:  {extra_pressure_max: 2, improvise_rounds: 2, improvise_commands: 6}
```

## Semantics

- **Times are `at_pct` of the event.** `at_min` is accepted and converted against
  THIS event's `duration_min` at validation, with the result written back — the
  shape of the packet's 7-hour ladder survives a 90-minute scrim.
- **The ladder supersedes the fixed grace window.** With the ladder enabled,
  `impact_started()` is "the tier allows some mechanism other than recon" (the
  `observe` tier is the discovery window). With the ladder disabled, the old
  `caps.grace_min` floor applies.
- **`modes` is the authority** for what a takedown may do (`stop`, `stop_mask`,
  `stop_rename`, `stop_break_config`, `stop_rename_config`) and for whether
  `block_port` is unlocked. `policy.allowed_impact_modes()` feeds
  `takedown.check()`; a mechanism above the tier is a `PolicyError`, so neither
  the model nor a hand-built candidate can jump the ladder.
- **`boxes_per_team`** widens the blast radius (`max_down_now()` is the max of
  `caps.max_down` and the tier's value): one box, then pairs, then a quarter of
  the board.
- **`pause_after_restore_min`** shifts the ladder clock back when blue restored
  something inside the window — the packet's mercy rule, implemented statelessly
  (the tier is re-derived from the shifted clock, so a restart resumes exactly).
- **`unrecoverable: true`** unlocks the unrecoverable family early (port blocks,
  credential rotation, lockout). Otherwise that family unlocks in the second half
  (`progress >= 0.5`). The tag on the effect is the single definition; there is
  no mode list to keep in sync.
- **Caps**: `max_down` is concurrent DOWN per team; `min_standing` is the floor of
  standing services per team (red eases off at the floor); `grace_min` is the
  discovery window when the ladder is off.
- **Windows** are minutes before the end: `endgame_teardown` is the final window
  (in it, the pacing gates are SUSPENDED — every mechanism legal, no per-team
  cap, a faster cadence), `lockout` is when the `lockout` effect is legal.
- **`budget`** bounds improvisation: how many extra pressure actions a cycle may
  take, and how much LLM-authored improvisation is allowed.

## The final window is not a separate component

The earlier agent had a coverage pass, an `endgame` sweeper and a burndown module
that each re-implemented takedown routing and had already diverged. Here the final
window is a policy block: the SAME loop, registry, gate and router run full-board
takedowns at `run.endgame_cycle_min` cadence, and the engine's `red-verdict`
proves the damage afterwards.
