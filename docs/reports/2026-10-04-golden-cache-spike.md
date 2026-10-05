# Golden cache — design spike and go/no-go (2026-10-04)

Implements the design spike required by [2026-10-04-hardening-plan.md](2026-10-04-hardening-plan.md)
§G0. **Decision: NO-GO for this cycle.** The design below is what a future implementation must
solve; nothing here is intractable, but every safe variant touches phase 4, terraform plumbing,
phase 1 reclamation, teardown sweeps, and the multi-node golden slots at once — and the failure
mode of getting one predicate wrong is the documented "foreign template squatting a golden
slot" incident class. Shipping that un-proven into the same scrim that validates eight other
fixes couples risks for a ~35 min/deploy saving. Revisit when there is a dedicated session for
it; the flag design below is the starting point.

## What the build does today (verified against source)

- Golden vmids are COMPUTED per competition: `engine_vmid + GOLDEN_VMID_OFFSET (150) +
  box_idx` (`constants.py:193`, `golden_ops.golden_vmid_for`), satellite slots shift by
  box-stride (`nodes_ops.golden_vmid_for_slot`).
- The build clones at that computed vmid, plants (nakon, strict, the expensive part),
  boot-smokes, converts (`qm template`), and stamps the content hash into the template
  DESCRIPTION plus the comp's `.template-hashes.json`.
- The M4 hash gate already short-circuits REBUILDS within the same competition: matching
  template hash → `build_golden_set` skips straight to reuse (`golden_ops.py:381-388`). The
  cache proposal is exactly this reuse, keyed across competitions.

## What a cross-comp cache must solve

1. **vmids.** A hit lives at the OWNER's computed slot. The next comp's slot at the same vmid
   is then occupied by a foreign template — unless the cache owns a DEDICATED vmid range
   (proposal: `cache_base = 1900..1950`, outside every computed block), and terraform's
   `golden_template_ids` receives the ACTUAL template vmids from the lookup instead of the
   computed ones (plumb-through exists: the build already returns `{box: vmid}`).
2. **Ownership.** Cache VMs carry `tezcatlipoca + golden-cache + hash-<h8>` and NO comp/run
   tags, so every existing destroy predicate refuses them by default (the fail-closed
   direction). `--full` must not touch them without `--purge-golden-cache`; GC is by hash
   (a cache entry whose hash no longer matches any comp's expectation is evictable) and by
   explicit purge.
3. **Eviction on drift.** A lineup change changes the hash → natural miss → fresh build →
   the OLD cache entry lingers until purged. Acceptable (disk cost, ~60G per Windows golden)
   with a documented `--purge-golden-cache`.
4. **Frozen comps.** The frozen drift gate refuses config drift for the COMP; a cache hit
   with a matching hash is not drift. No interaction, but the freeze test suite must pin it.
5. **Multi-node.** Satellite slots (`golden_vmid_for_slot`) and jump-routed plants mean the
   cache is per-NODE (a hash hit on the wrong node is a miss). Tag the cache VMs with the
   node name too.

## If picked up later

`TEZ_GOLDEN_CACHE=1`, default off. Phase 4: compute expected hashes first (already happens
for the M4 gate), look up `hash-<h8>` templates in the cache range, on hit stamp the comp's
`.template-hashes.json` and return the vmid map; on miss build as today and, instead of
`--full`-destructible comp tags, stamp cache tags. The scrim-style validation is deploy-only:
deploy comp A (builds), `--full` (cache survives), deploy same-lineup comp B — phase 4 wall
time < 5 min and zero `qm template` conversions in the timing sidecar.
