# Prompt — bad-auto session isolation, or vendoring (2026-10-12)

Copy everything below the line into a fresh session.

---

You are eliminating a cross-session shotgun that already fired once: **tezcatlipoca
and bad-auto share ONE bad-auto checkout and ONE repo-root config, so two parallel
sessions deploying ranges from two worktrees clobber each other's red01 identity
state — and on 2026-10-12 a validation deploy followed the shared `config.yaml`,
recycled vmid 999, and killed another session's in-flight red01 mid-event.** The
owner has pre-approved the fix in either of two shapes (§Decision) — you do not
need permission for the work, only to record which shape you chose and why.

The project's first discipline is in AGENTS.md: fix causes, not symptoms; complexity
belongs in the tool; anything an operator must "remember" is a bug filed against the
code. "Coordinate with the other session about config.yaml" is not a fix — it is
the bug. The 2026-10-12 teardown redesign (main 3d1664e) is merged and live; its
bad-auto contract (TeardownRefusal / `TEARDOWN-REFUSAL` line / exit 17 /
evidence-before-destroy — bad-auto main 78d3f46) is the vocabulary you build on,
never break.

## What exists today (verify, don't trust this list)

- **The shared surface.** tezcatlipoca resolves the sibling checkout as
  `REPO.parent/"bad-auto"` (`red_plant_ops.BAD_AUTO`, `scrim/core.BAD_AUTO`) — the
  worktree rule means every tezcatlipoca worktree resolves to the SAME bad-auto
  checkout. bad-auto's `config.yaml` lives in ITS repo root and carries the deploy
  identity: `deploy.red_vmid` (default 999 — a SHARED node slot), `deploy.red_ip`,
  `engine_ip`, `competition_dir`, the realm block. Whoever deployed last owns it.
- **The stamp.** bad-auto stamps `.deploy-stamp.json` in its repo root
  (`red_vm.py`, `STAMP_NAME`) — one global stamp, clobbered by every deploy; a
  second checkout (e.g. a bad-auto worktree) sees "no deploy stamp — identity rests
  on the VM description marker". The 3-way identity check (config / stamp / live
  marker) is correct; its STATE is global.
- **The seam already exists.** `badauto.config.load_config(path)` accepts
  `BAuto_CONFIG` env or `--config`; `red_plant_ops._deploy_red` ALREADY passes a
  derived config via `--config` (realm_c2_ops's `prepare_local_c2` writes it) and
  scrubs `BAuto_CONFIG` from the env when it does. tezcatlipoca simply never uses
  the seam for its own config — every call site leans on the conventional
  repo-root file.
- **Call sites to thread** (find any others): `red_plant_ops.py` (BAD_AUTO,
  `_configured_red_ip/_configured_red_vmid`, `_deploy_red`, `destroy_red`,
  `red_vmid_candidates`), `scrim/core.py` + `teardown_stage.py` + `red_link.py`
  (BAD_AUTO constant, `procs.run(cwd=BAD_AUTO)`, config reads),
  `realm_c2_ops.py` (derived-config flow), and the teardown engine's RedStep /
  `teardown/runner.py` via `red_plant_ops`.
- **red01's node footprint** is also shared-by-default: vmid 999 and
  red_ip 10.0.0.198/.199 are outside the collision-gated space the AGENTS.md
  concurrency rule covers (scoring vmid, team identifiers, mgmt IP). The
  preflight gates none of it.
- Tests: tezcatlipoca main 1433 green (hermetic, ~50s); bad-auto main 563 green.
  Both suites run with plain pytest, no fixtures layer.

## Non-negotiable invariants

- **One deploy, one identity.** Two sessions may deploy simultaneously (distinct
  scoring vmid, identifiers, mgmt IP) and MUST NOT be able to see, overwrite, or
  destroy each other's red01 identity state — config, stamp, derived realm config,
  failure logs. This must hold with zero operator coordination.
- **red01 joins the collision-gated space.** Its vmid and IP must be per-comp
  reserved and preflight-gated like the scoring engine's (a second session
  defaulting to vmid 999 is a preflight REFUSAL, not a warning).
- The 3-way identity check, the teardown contract (structured refusals + exit 17
  + evidence-before-destroy), `BAuto_COMPETITION_DIR`, absolute comp-dir paths,
  and the realm-C2 derived-config flow survive unchanged — generalize the derived
  config pattern; do not duplicate it.
- A tezcatlipoca worktree must work with NO sibling bad-auto config state at all
  after this: fresh clone or fresh worktree → deploy → red01 → destroy works
  end to end.
- Both suites green at every commit; the characterizations you write in step 1
  pass before and after.

## Decision (yours to make, recorded in the design doc)

**Option A — per-session config + stamp isolation (recommended).** tezcatlipoca
writes a per-competition bad-auto config (natural home: `competitions/<id>/`
— it is per-deploy deploy identity, like teams.json) and threads it through
every badauto invocation via the existing `BAuto_CONFIG`/`--config` seam; bad-auto
moves the stamp next to the config (or into the comp dir) so it is per-session;
red01's vmid/IP come from the comp's reserved block and are preflight-gated.
Root-cause fix for the incident; days not weeks; bad-auto keeps its repo, CI and
history. Costs: every call site threads a path; two repos remain.

**Option B — move bad-auto into tezcatlipoca.** Vendor it as a subpackage (keep
its git history via subtree or a preserve-then-archive import), one config root,
no sibling resolution, the subprocess boundary can eventually become an import.
Costs: moving a repo that parallel sessions are ACTIVELY using mid-scrim-season is
disruptive (coordinate the cutover or stage it: vendor first, deprecate the
sibling checkout after a soak); ~563 tests migrate; release cadence couples to the
range driver. If you choose B, stage it so no in-flight session breaks, and say
why the merge is worth the coordination cost right now.

Escalate only if you find a true fork beyond these two (e.g. changing what a
bad-auto deploy IS). Otherwise: decide, record the reasoning in the design doc,
ship.

## Process

1. **Pin the hazard first.** Characterization tests that prove the CURRENT
   cross-session clobber (offline, faked): two "sessions" (two config paths +
   comp dirs) driving the same bad-auto checkout → show config/stamp overwrite;
   the stamp-global clobber; the vmid-999 default collision as preflight-blind.
   These fail against today's code in the ways that matter and must pass after.
2. **Design doc** `docs/bad-auto-isolation.md`: chosen option, the config/stamp/
   vmid lifecycle, every call site, what changes in bad-auto, what does not.
3. **Implement incrementally**, both suites green at every commit, bad-auto
   changes on its own branch (it keeps its own repo even under Option A).
4. **Live-validate from a new worktree** (practice-run rule): two concurrent
   deploys from two tezcatlipoca worktrees with isolated configs — both red01s
   up on distinct vmids/IPs simultaneously, no stamp/config clobber (assert the
   stamps differ), one destroyed, the other's red01 untouched and still verified;
   a fresh worktree with zero shared state does the full cycle; the preflight
   refuses a colliding red01 vmid. Real collection/destroy ordering exercised.
5. **Drain the docs**: AGENTS.md concurrent-ranges rule gains red01's vmid/IP and
   the config rule; docs/environment-facts.md red01 facts; any "coordinate about
   config.yaml" advice anywhere is deleted — the tool must not need the advice.

## Coordination

A test-loop session may be running scrims in the main tree — never destroy or
pause anything you did not create; merge between its runs after both suites and
the live proof are green; push both repos (user directive). The node is shared:
your validation comp uses a scoring vmid, identifiers, mgmt IP AND a red01
vmid/IP that collide with nothing live; preflight must pass clean.

## Deliverables

- The isolation mechanism (or the staged vendor), both suites green, characterizations migrated and passing.
- docs/bad-auto-isolation.md with the decision, reasoning, and the live-validation transcript.
- Docs drained; AGENTS.md shorter on coordination advice, longer on guarantees.
- Definition of done: two sessions can deploy ranges with red01 concurrently
  from isolated worktrees with zero shared bad-auto state and zero coordination —
  and a third session reading either config.yaml sees only its own deploy's
  identity.
