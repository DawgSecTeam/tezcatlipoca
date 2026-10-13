# bad-auto session isolation — design + validation (2026-10-12)

One incident drives this document: on 2026-10-12 a validation deploy followed the
shared bad-auto `config.yaml`, recycled vmid 999, and killed another session's
in-flight red01 mid-event. This is the root-cause fix, the decision record, and
the live-validation transcript.

## The hazard, pinned before the fix

Every tezcatlipoca worktree resolves the SAME sibling bad-auto checkout
(`REPO.parent/"bad-auto"` — `red/red_plant_ops.py`, `scrim/core.py`,
`red/realm_c2_ops.py`), and bad-auto keeps its deploy identity in TWO repo-root
files of that one checkout:

- `config.yaml` — `deploy.red_vmid` (default **999**, a shared node slot),
  `deploy.red_ip`, `competition_dir`, the realm block. Whoever staged last owns it.
- `.deploy-stamp.json` — the deploy ground truth destroy's 3-way identity check
  reads (config / stamp / live VM marker).

The kill vector is worse than a mis-aimed destroy: `deploy_red_vm` is
clone-or-reuse — a live VM at the configured vmid is "already exists —
reconfiguring" (`badauto/deploy/red_vm.py:607-608`) and gets re-stamped with the
new deploy's `instance_id`. Session B's deploy at the shared vmid finds A's live
red01, reconfigures it, and rewrites the stamp; from then on every leg of the
3-way check agrees the VM is B's. **The check is correct logic over globally
clobbered state.**

An offline demo (`/tmp/clobber-demo.py`, run 2026-10-12 against the pre-fix
trees) reproduced every leg hermetically:

```
== session A stages its red01 (vmid 1470 @ 10.0.0.198) ==
   A resolves its red01: vmid=1470 ip=10.0.0.198
== session B stages its red01 (vmid 1480 @ 10.0.0.199) — same checkout ==
== what session A's destroy will now act on ==
   A resolves its red01: vmid=1480 ip=10.0.0.199
   >>> CLOBBERED: A's identity resolution now names B's target.
== the deploy stamp: one file for the whole checkout ==
   after A deploys: instance_id=aaaa vmid=1470
   after B deploys: instance_id=bbbb vmid=1480
   >>> CLOBBERED: A's destroy ground truth no longer exists anywhere.
== preflight: is any of this gated? ==
   engine=1300, identifier=130 -> gated slots: [1300, 1440, 1500, 1450]
   >>> red01's vmid appears in NO gate — a second session defaulting
   >>> to 999 sails through preflight straight into the reuse path above.
```

The live estate showed the same shape the morning after: `config.yaml` named
scrim-one (rewritten 10:49) while `.deploy-stamp.json` still named the deleted
teardown-redesign worktree's tdred-2026 (08:36) — two sessions, one file each.

Additional readers that follow the singleton and therefore cross sessions:
`scrim/red_link._red_ssh_spec` **overwrites** the CLI `--red-ip` with whatever
the singleton currently says (redirecting a run's collector at another session's
red01); `scrim/teardown_stage._config_red_vmid` reconciles against it;
`scrim/red_stage.py:215` fills a missing `--red-vmid` by regex-reading bad-auto's
SOURCE for the 999 default.

## Decision: Option A — per-competition config + config-scoped stamp + red01 preflight gates

The owner pre-approved either shape. **Option A chosen.** Reasoning:

- It threads a seam that already exists and is already live-proven:
  `load_config` takes `--config`/`BAuto_CONFIG` (precedence: explicit path >
  `BAuto_CONFIG` > repo-root conventional > DEFAULTS), and the realm-C2 flow
  (`realm_c2_ops.prepare_local_c2` → `_deploy_red --config`) has driven per-comp
  derived configs since 2026-10-08, including the missing-path refusal that
  guards bad-auto's silent fall-back-to-DEFAULTS.
- It fixes the actual root cause — deploy identity living in globally-shared
  mutable state — with a small blast radius per call site.
- bad-auto keeps its repo, CI, and history; both suites stay hermetic.

Option B (vendoring bad-auto into tezcatlipoca) was rejected **for now**: moving
a repo that parallel sessions actively use mid-scrim-season, migrating its 570
tests, and coupling its release cadence to the range driver — when the pain that
motivates vendoring (shared mutable identity state) is eliminated by A. The
remaining shared surfaces after A are read-only (see below); if they ever bite,
the vendoring case can be re-made from a clean slate.

## Design

### One per-competition bad-auto config

`competitions/<id>/.badauto-config.yaml` (gitignored) is THE bad-auto config
tezcatlipoca uses. It supersedes both tez's use of bad-auto's repo-root
`config.yaml` and the realm flow's `.realm-c2-config.yaml` — the realm block is
a layer applied by the same builder (`realm_c2_ops.apply_local_c2`), not a
second file. bad-auto's own repo-root `config.yaml` remains for standalone
bad-auto use; tez never reads or writes it again.

- Written by ONE preparer (new `red/badauto_config_ops.py`) used by both red-touching
  paths: deploy phase 7 (`red_plant_ops.plant_assume_breach` — previously only
  prepared a config when local-C2 was on) and the scrim harness
  (`scrim/red_stage.stage_red`, which owns the llm/pacing/event blocks and calls
  the preparer's writer).
- Carry-over ("blackbox") blocks — realm/beacon/evasion/naming and operator
  deploy keys the stage does not own — are carried from the COMP'S OWN prior
  config, never the sibling singleton. First run on a fresh comp starts from {}
  and bad-auto's DEFAULTS (load_config deep-merges), with `apply_local_c2`
  synthesizing the full realm block when the Compfile enables it.
- Every badauto invocation threads the config explicitly (`--config <abs path>`,
  with the missing-file refusal already proven in `_deploy_red`) and scrubs a
  stale `BAuto_CONFIG` from the child env, symmetric with `_deploy_red`.

### red01 joins the reserved, collision-gated space

- vmid: `red01_vmid_for(comp, engine_vmid)` = Compfile `red01_vmid`, else
  derived `engine_vmid + 170` (clear of the engine template +140, goldens +150,
  and multi-node jump +130 offsets). No tez path falls back to bad-auto's
  built-in 999 anymore; the `bad_auto_deploy_default` source-regex is deleted.
- IP: Compfile `red01_ip` (with `assume_breach_red_ip` still honored), else the
  ONE tez default `10.0.0.198` (the old .198/.199 split between scrim and the
  deploy path is gone).
- Recorded at every layer that later needs it: the comp config's `deploy` block,
  `.deploy_state.json`'s `assume_breach` record (now carries `vmid`), and the
  scrim manifest — complementing the teardown-time target-line parse.
- Preflight (new `preflight/red01.py`, run from `run_preflight` beside the
  mgmt-IP gates):
  - **vmid gate** — a live VM at the planned red01 vmid is a REFUSAL unless this
    is a resume and the occupant's bad-auto description marker names THIS comp
    (the marker is shown in the error — that is how you find whose it is, no
    "coordinate with its session" required).
  - **IP gate** — cluster-wide running-guest agent scan (same shape as the
    engine mgmt-IP gate): a guest answering the planned red01 IP that is not
    this comp's red01 is a REFUSAL; unverifiable guests refuse the DEFAULT IP
    and only warn on an explicitly-set one.

### The stamp follows the config (the only bad-auto behavior change)

`badauto/config.py` gains `resolve_config_path(path=None)` (the file load_config
would read, or None). `_write_stamp`/`_load_stamp` operate on the directory of
the loaded config — explicit `--config`/`BAuto_CONFIG` or conventional
repo-root alike — falling back to the repo root when no config file was loaded.
The standalone flow (no explicit config) is byte-identical to before, because
the conventional config lives in the repo root. Two sessions therefore hold two
stamps with two `instance_id`s, and each destroy consults only its own.

`destroy-failure.log` stops being a shared repo-root file: cmd_destroy writes it
next to the loaded config (repo root for the standalone flow) and it is
untracked/gitignored.

### What must NOT change (pinned by tests before and after)

- The 3-way identity check and its refusal matrix (stamp/marker/comp mismatch).
- The teardown contract: `TEARDOWN-REFUSAL` machine line, exit 17,
  evidence-before-destroy.
- `BAuto_COMPETITION_DIR`, absolute comp-dir paths, `--competition` as a
  cross-check (not an override) in destroy.
- The realm-C2 derived-config semantics (retarget + synthesize + completeness
  refusal) — same layer, same file family, per-comp location.
- `load_config` precedence and the silent-missing-path-to-DEFAULTS behavior
  (characterized; tez guards it by refusing a missing config BEFORE invoking).

### Accepted residuals (read-only, documented)

- The bad-auto CODE checkout itself is shared by all worktrees (read-only).
- `BAD_AUTO/.env` (`BAuto_LLM_API_KEY`) is shared, read-only.
- bad-auto's `../tezcatlipoca/.env` setdefault fallback for Proxmox creds —
  read-only, and the deploy environment always exports the worktree's own
  `TF_VAR_proxmox_*` trio, which wins.

## Call sites threaded

| Site | Change |
|---|---|
| `red_plant_ops._configured_red_ip/_configured_red_vmid` | read the COMP config |
| `red_plant_ops.red_ip_for` / `red_vmid_candidates` | Compfile → comp config → default; state candidate now populated |
| `red_plant_ops._deploy_red` | unchanged (already threads `--config`); now always given one |
| `red_plant_ops.destroy_red` | passes `--config` when the comp config exists (never a missing path) |
| `red_plant_ops.plant_assume_breach` | resolves red01 vmid, ALWAYS prepares the comp config, records `vmid` in state |
| `scrim/red_stage.stage_red` | writes the comp config; `--config` on validate-llm/run/deploy; vmid from resolution, not the source-regex |
| `scrim/red_link._red_ssh_spec` | resolves from the run's own record (manifest/comp config), never the singleton; the CLI `--red-ip` flag is no longer silently overwritten |
| `scrim/teardown_stage` | `--config` on destroy; `_config_red_vmid` reads the comp config; the final 999-regex fallback is gone |
| `realm_c2_ops.build/write/prepare_local_c2` | base = comp config; writer targets `.badauto-config.yaml` |
| `teardown/steps.py` RedStep / `teardown/engine.py` | ride `red_vmid_candidates` |
| `preflight/gate.py` + `plan.py` | red01 gates; `PreflightPlan.red01_vmid/red01_ip` |

## Live validation (2026-10-12, cyberrange .150 — transcript)

Two FRESH worktrees (`badauto-isolation`, `badauto-isolation-b` — the second cut
only to give the second deploy its own comp dir) drove the one shared bad-auto
checkout on branch `isolation-2026-10-12`. Comps: `iso-a-20261012` (scoring
1300, identifier 130, engine mgmt .253, red01 derived 1470 @ .198) and
`iso-b-20261012` (scoring 1600, identifier 131, engine mgmt .254, red01 1770 @
.199 via the Compfile knob), both one-box `base-ubuntu24.04-fix` + apache,
`assume_breach 1`, full eight-phase deploys with local-C2 tavern on red01.

1. **Overlap.** B's deploy ran while A's range was fully up. Both finished:
   A red01 vmid 1470 @ 10.0.0.198, B red01 vmid 1770 @ 10.0.0.199, both
   `running` simultaneously.
2. **Stamps isolated.** `competitions/<id>/.deploy-stamp.json` next to each
   comp's own `.badauto-config.yaml`: instances `93389c2e5d54` vs
   `51529173a9cd` — distinct files, distinct identities, configs agreeing with
   stamps. The shared checkout's repo-root `config.yaml` and stale
   `.deploy-stamp.json` (md5-baselined BEFORE either deploy) are byte-identical
   after both deploys — nothing in the new flow touches them.
3. **The gates fired live, twice.** B's first attempt (scoring 1310) was
   refused by the EXISTING collision gate — its engine-template slot 1450
   collided with A's golden block 1450 (scoring vmids +10 apart overlap; leave
   room for engine+140…+170). Relanced at 1600. And a third comp aimed at
   A's red01 vmid (`red01_vmid 1470`, scoring 1301) was refused:
   `ERROR: red01 vmid 1470 is already occupied by vmid 1470 (bad-auto-red01 on
   proxmox), bad-auto marker comp=iso-a-20261012. This is the 2026-10-12 kill
   shape…` — nothing created, owner named, no coordination required. (Its
   first attempt was also refused by the engine mgmt-IP gate — .253 answers —
   correct gate ordering: engine gates before red01.)
4. **One red-flag fix, applied as designed.** A's first launch was refused by
   the red01 IP gate: three standing workshop guests have no working agent, so
   the DEFAULT red01 IP was unverifiable → refusal (the engine gate's
   unverifiable-default rule). Pinging .198/.199 clean and pinning them in the
   Compfiles (the explicit-operator path) let both deploys proceed.
5. **Destroy A → B untouched.** A's full teardown ran its RedStep through A's
   comp config (`[red01] ok — red01 destroyed`; vmid 1470 absent from the
   cluster); B's red01 (1770) still running with its description marker
   instance `51529173a9cd` matching B's own stamp.
6. **Destroy B → estate verified clean.** Both teardowns exited with the
   verify-clean verdict; no validation vmid remains on the node. Both comps'
   artifacts auto-archived to `~/.tezcatlipoca/automated-tests/`.

Suites at validation time: tezcatlipoca 1455 green, bad-auto 572 green.

**Definition of done, met:** two sessions deployed ranges with red01
concurrently from isolated worktrees sharing ONE bad-auto checkout, with zero
shared bad-auto state and zero coordination; a reader of either comp's config
sees only its own deploy's identity; the preflight refuses a colliding red01
slot by name.
