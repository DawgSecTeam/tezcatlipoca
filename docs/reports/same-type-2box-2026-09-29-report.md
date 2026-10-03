# same-type-2box-2026-09-29 — validation comp for multi same-TYPE pins per box

One team × two boxes (web01 ubuntu24.04-fix 30G, win01 base-windows-server sata0 60G, no
AD/domain_roles), 6 scored pins, no planted vulns, engine vmid 1090 (template 1230, goldens
1240/1241), team ident 120. Config in `competitions/same-type-2box/` (committed);
range torn down `--full` at close. Validates the dedup fix from tezcatlipoca e026671 +
bad-auto 6c554ba (coverage display overrides).

## The regression shape, closed

regression-4x1's open finding was: two same-check-TYPE pins on one box (apache + roundcube,
both `("Web", 80)`) collapsed to one scoreboard check — `web01-roundcube` never registered
(12 pins → 11 checks). Root cause was tezcatlipoca's own per-box `(TYPE, port)` dedup in
`build_event_conf`, NOT upstream Quotient (whose `Box` config holds slices per check type and
only requires unique `<box>-<Display>` names — the original known-issues attribution was
wrong and has been rewritten).

This comp re-runs that exact shape and adds the Windows leg:

| Box | Pins | Scoreboard checks |
|---|---|---|
| web01 | `apache`, `roundcube`, `bind` | `web01-http` + `web01-roundcube` (two Web, one box — the collapse case) + `web01-dns` (control) |
| win01 | `Enable WinRM`, `IIS HTTP`, `{"name": "IIS HTTP", "display": "iis-alt"}` | `win01-winrm` (control) + `win01-iis` + `win01-iis-alt` (two Web via the per-pin display override — the only way to express two Windows Web checks with the current catalog) |

## What it validated

- **6 pins → 6 scoreboard checks** (`/api/services`, 9+ scored rounds all green at first
  verify): the historical 12→11 failure mode is gone. Identity is now `(box, Display)`;
  duplicate Displays on a box are a generate-time `SystemExit`, never a silent drop.
- **Per-pin check overrides work end-to-end**: the dict pin plants ONCE (nakon machine list
  strips `display`; catalog check reports "3 selected → resolves to 2 step(s)" for win01)
  but scores TWICE under distinct ServiceNames.
- **`verify --strict-services --expect-no-vulns`: RESULT PASS**, including the new
  `pins_registered` gate ("EXPECTED-PINS all 6 pinned checks registered") — the gate that
  would have caught 11-for-12.
- **bad-auto coverage: 6/6 taken down + restored** (`coverage-run1.json` in the comp dir):
  `web01-roundcube` — the row that could never flip in regression-4x1 (baseline=0 forever) —
  now flips via the roundcube→apache2 alias, and `win01-iis-alt` flips as its own display
  via the W3SVC windows postcheck. Coverage display derivation handles dict pins (bad-auto
  6c554ba); without it both IIS rows would have keyed `win01-iis` and the alt row would have
  passed off its sibling's flip.
- Deploy ~25 min end-to-end on .193 (no AD, ident 120 dodges the 1210/1211 challenge-template
  vmid collision; scoring vmid 1090, engine template 1230, goldens 1240/1241 — all free).

## Live-found defect

- verify's `pins_registered` gate initially computed an EMPTY expected set: it derived box
  names from `load_boxes()` (nakon-config machines, team-suffixed `web01-team120`) instead of
  `boxes.json` (box types, which `box_services.json` keys on). Fixed in 1f9364c. The gate
  failing OPEN (empty expected set skips the gate) is the failure mode to remember: it needs
  a comp with same-TYPE pins to notice.

## Operational notes

- red01 for coverage: vmid 999 (`.244`), reused after the orphaned shakedown red01 that
  still occupied it was destroyed via the matching `badauto destroy --competition
  .../shakedown-5x4-2026-09-28 --yes` (config.yaml still named that comp). coverage runs
  from `/opt/bad-auto` on red01 (`cd /opt/bad-auto && sudo python3 -m badauto coverage ...`)
  — `/usr/bin/python3 -m badauto` from $HOME does not see the module.
- Closeout hit a NEW facet of the config.yaml trap: plain `badauto deploy` never updates
  host-side config.yaml (that's run-agent-scrim's job), so destroy's matching-dir guard
  compared against the stale shakedown entry — whose vmid 999 had recycled onto MY red01.
  Resolved by verifying red01's on-box `quotient.base_url` pointed at this comp's engine,
  repointing config.yaml's `competition_dir`, then the matching destroy (see known-issues
  badauto entry).
- Both apache/roundcube rows and both IIS rows flip TOGETHER (shared unit per box): the
  scoreboard independence proven here is per-check registration + per-display tracking, not
  per-service process isolation (roundcube is an apache vhost; the alias model is correct).
