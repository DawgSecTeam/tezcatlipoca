# M0.2 benchmark — linked vs full clones, and the v2 pipeline's real numbers

Date: 2026-09-25 (bench-parallel-2026-09-24, 2 teams × 3 Linux boxes, difficulty 3).
Verdict up front: **M3 (golden templates + linked clones) is a GO.** The full v2
pipeline ran end-to-end on the realm, verified PASS, torn down clean. Linked clones
remove the pipeline's storage ceiling; nothing in the runs reproduced the HTTP 596 /
pvestatd hazard, including parallel starts and a parallel destroy of a live range.

## Environment (and why the numbers are conservative)

The bench ran on **realm** (10.0.0.150, node `proxmox`, datastore `hdd` zfspool) because
the primary node went offline mid-session; it is a *shared, busy* host (workshop VMs,
~30 running guests), and — decisively — **its PVE guest-agent answers pings but returns
NULL for every data call**. bpg's agent-based IP discovery is therefore impossible there,
which is why the engine now takes a static mgmt IP (`engine_mgmt_ip`, commit 1848771) and
why some bpg team-box creations paid a multi-minute agent-wait timeout (one box: 15m9s in
an early attempt). On the primary — where the agent channel works — apply #2 should be
* faster and steadier* than the numbers below, not slower.

## The successful run (timings from .deploy-timings.jsonl)

| Phase | Op | Wall |
|---|---|---|
| 1 | destroy_vm ×10 (parallel, live range incl. engine) | **36 s total** |
| 1 | destroy_bridge ×2 | 0.2 s |
| 2 | terraform apply #1 (engine full clone + bridges + netplan + cold boot) | **165 s** |
| 3 | engine bootstrap (docker, Quotient build+up, apt-cacher-ng) | 459 s |
| 3 | push_event_conf | 13 s |
| 4 | golden build: 3 full clones + waits + passwords + prep + snapshots + strict plant + convert | (phase-4 ops below) |
| 4 | terraform apply #2 — 6 linked clones, parallelism=1 | **708 s across 2 applies** (first pass ≈ 8 min; a fixup replacement pass ≈ 4 min) |
| 4 | per-box waits (SSH + cloud-init, concurrent) | 5 s |
| 4 | prep_apt (concurrent, readiness-probed) | 516 s |
| 4 | snapshots (concurrent ×12) | 135 s |
| 5 | repair sweep (nakon, jobs=4, 2 machines) | **3.4 s** |
| 5 | fix_services (concurrent) | 19 s |
| 6 | final pass (nakon, 2 machines) | **3.4 s** |
| 6 | tz-ready snapshots (concurrent ×12) | 66 s |
| 7 | seed + unpause | 0.5 s |
| | **Measured total** | **≈ 35.5 min** |

## Linked vs full — the comparison the plan asked for

- Full clones (measured on this node's zfs pool, this session): 10–15 GB Linux ≈
  1m30s–1m45s each, strictly serial (datastore rule). The v1 pipeline paid that twice —
  team1 in apply, then team2+ in phase 6 — plus a 60 GB Windows budget of 1800 s *per
  box*, plus a full nakon plant on every team.
- Linked clones (measured): **24 s–2m5s per box for the whole VM create** (clone + start +
  bpg waits), serial, and the same disk is shared read-only by every team. The golden
  plant — the expensive part — runs **once per box type**, not per team.
- The v1 shape's costs are structurally gone: no phase-6 bulk clone fan-out, no per-team
  nakon plant of service installs. What remains per team is two tiny passes (3.4 s each
  here) that plant only disruption/boot-hostile flavor.

## 596 / pvestatd watch

Zero 596s or pvestatd hangs across every run this session, including: parallel cleanup of
a live 10-VM range, bounded parallel starts (4 workers), and two full golden rebuilds.
Consistent with the hazard model: it belongs to concurrent bulk clone *writes*, which the
v2 pipeline no longer does at all.

## What the validation flushed out (all fixed on this branch)

- Stale cross-node terraform state poisons applies (refresh targets the old node's name) —
  per-comp state is now endpoint/vmid-guarded (a33faf3) and the retry path tolerates
  tagged-as-ours leftovers in preflight + cleanup.
- PVE tag separators differ between views (`;` vs `,`) — both parsers accept both.
- `run_concurrent` keyed results by item (unhashable target dicts) — now index-aligned.
- `systemd-system-masked` baked into a golden disk left every linked clone bootless —
  boot-hostile configs moved to the final stage (constants.py), planted after domains.
- Golden templates are keyed **positionally per box**, not by template name: two box
  types sharing a base template silently cross-wired golden disks (web01 clones got
  golden-db01's content) (296c837).
- API clones bake no cloud-init identity — golden_ops sets ciuser/cipassword/sshkeys and
  resets passwords pre-plant.
- `qm template` refuses VMs holding snapshots — tz-base is deleted before conversion.
- Engine bootstrap needs `apt -f install` for half-configured base images.
- Open (upstream; the driver half is closed): the randomize→pin flow drops required catalog vars —
  [upstream-defects-handoff.md](upstream-defects-handoff.md).

## Residual risks before a real competition

1. **Domain-roles interaction is now covered by M4 validation**: the repair/final stage
   split keeps disruptive + boot-hostile flavor *after* domain joins, while DCs use unbooted
   goldens so each team's forest gets a unique DomainSID. ADWS readiness is awaited before
   AD-aware misconfigs, DNS SRV readiness before member joins, and transient joins are probed
   and retried.
2. The M4 Windows/domain run confirmed the Windows golden path, but the accepted lifecycle
   decision remains that linked-cloned member machine SIDs may duplicate; only DomainSID
   uniqueness is gated because isolated per-team forests do not share member local accounts.
3. The primary node was down all session; its leftovers (bench VMs 1090/1220–1222 from
   the first attempt) still need a phase-1 cleanup pass when it returns.
