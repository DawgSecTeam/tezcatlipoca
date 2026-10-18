"""Seeding: assume-breach as a pipeline stage, not loop logic.

`assume_breach: full` means red is ALREADY inside when the event starts — the
doctrine the club grades red behaviour against. Seeding is therefore a stage of
the deploy pipeline (run after the clones are up and before T0), and it runs
through the SAME effect registry and gates as the live loop: dry-runnable,
re-runnable, and unable to do anything the live loop could not.

Depth tiers (per box, in order):

    0  access only          (foothold via known creds)
    1  + escalation         (root/admin path)
    2  + channel            (C2 implant, verified by callback)
    3  + persistence/evasion (one persistence family + one evasion touch)

`assume_breach: light` = channel only (a foothold is established if needed, but
nothing else is planted). `off` = cold start, practice only.

Every box's record lands in `world.meta["seed"][ip]` (resumable), and the run
writes `competitions/<id>/red/manifest.json` — what is planted where, with the
restore recipe — which `red-verdict`, destroy and the organizer report consume.
"""

import json
from pathlib import Path

from .effects import execute
from .state.schema import atomic_write, now_iso

DEPTH_STAGES = ("access", "escalate", "channel", "persist")

# One evasion touch per box at full depth: families whose point is making blue's
# hunting harder rather than adding a new channel.
EVASION_FAMILIES = ("immu", "timestomp", "logwipe", "preload", "decoy")


def plan_for(cfg, world, intel, depth=None, teams=None):
    """The seed plan: one entry per box that needs work, with its next stage."""
    depth = int(cfg["red"].get("depth", 3) if depth is None else depth)
    out = []
    for box in intel.boxes():
        if teams and box["team"] not in teams:
            continue
        ip = box["ip"]
        record = world.seed_record(ip) or {}
        stages = list(DEPTH_STAGES[:depth + 1])
        done = set(record.get("stages") or [])
        todo = [s for s in stages if s not in done]
        out.append({"ip": ip, "box": box, "depth": depth, "todo": todo,
                    "record": record})
    return out


def seed_range(ctx, *, depth=None, teams=None, dry_run=False, max_steps=None,
               manifest_dir=None):
    """Run the seed. Returns a report dict; every action goes through execute()."""
    from .policy import Policy

    cfg = ctx.cfg
    mode = cfg["red"].get("assume_breach", "full")
    if mode == "off":
        return {"mode": "off", "boxes": {}, "note": "assume_breach off — nothing seeded"}
    if mode == "light":
        depth = 2 if depth is None else min(int(depth), 2)

    policy = Policy(cfg, ctx.world, ctx.intel, sensors=None, rng=ctx.rng)
    ctx.policy = policy
    if not ctx.world.meta.get("event_start"):
        ctx.world.ensure_event_start()

    plan = plan_for(cfg, ctx.world, ctx.intel, depth=depth, teams=teams)
    # A previous run that ended with work outstanding must not make this one a no-op:
    # seed_done used to be set even when every stage failed, and the loop below breaks on
    # it (live: the deploy's planted-box guard kept failing a range whose re-seed never ran).
    if not dry_run and ctx.world.seed_done() and any(e["todo"] for e in plan):
        ctx.world.set_seed_done(False)
    steps = 0
    results = {}
    for entry in plan:
        ip = entry["ip"]
        record = dict(entry["record"])
        record.setdefault("stages", [])
        record["box"] = entry["box"]["box"]
        record["os"] = "windows" if entry["box"]["windows"] else "linux"
        for stage in entry["todo"]:
            if max_steps is not None and steps >= max_steps:
                break
            if ctx.world.seed_done():
                break
            outcome = _run_stage(ctx, ip, stage, mode, dry_run=dry_run)
            record.setdefault("actions", []).append(
                {"stage": stage, "effect": outcome.get("effect"),
                 "ok": outcome.get("ok"), "detail": outcome.get("detail")})
            if outcome.get("ok"):
                record["stages"].append(stage)
                if stage == "escalate" and outcome.get("escalation"):
                    record["escalation"] = outcome["escalation"]
                if stage == "channel":
                    record["channel"] = True
            steps += 1
        # persistence at full depth: one family plus one evasion touch
        if mode == "full" and (depth is None or depth >= 3) and "persist" in record["stages"]:
            evasion = _run_evasion(ctx, ip, dry_run=dry_run)
            if evasion:
                record.setdefault("actions", []).append(evasion)
                record["evasion"] = evasion.get("effect")
                steps += 1
        ctx.world.set_seed_record(ip, record)
        results[ip] = record
        if ctx.world.seed_done():
            break

    if not dry_run:
        ctx.world.set_seed_done(True)
    manifest = write_manifest(ctx, results, mode=mode, depth=depth,
                              out_dir=manifest_dir)
    return {"mode": mode, "depth": depth, "boxes": results, "steps": steps,
            "manifest": manifest}


def _run_stage(ctx, ip, stage, mode, dry_run=False):
    if stage == "access":
        if ctx.world.foothold(ip):
            return {"stage": stage, "effect": None, "ok": True,
                    "detail": "foothold already present"}
        result = execute("spray_access", {"ip": ip}, ctx, source="seed")
        if not result.ok:
            # a foothold may already exist through a key we hold; try the
            # eviction-recovery path second, but never fake success
            fallback = execute("reestablish", {"ip": ip}, ctx, source="seed")
            return {"stage": stage, "effect": "spray_access",
                    "ok": bool(fallback.ok),
                    "detail": f"spray: {result.detail} | reestablish: {fallback.detail}"}
        return {"stage": stage, "effect": "spray_access", "ok": True,
                "detail": result.detail}
    if stage == "escalate":
        result = execute("escalate", {"ip": ip}, ctx, source="seed")
        foothold = ctx.world.foothold(ip) or {}
        return {"stage": stage, "effect": "escalate", "ok": result.ok,
                "escalation": foothold.get("escalation"),
                "detail": result.detail}
    if stage == "channel":
        if ctx.c2 is None:
            return {"stage": stage, "effect": "implant_channel", "ok": False,
                    "detail": "c2 not enabled — skipped"}
        if mode == "light" and not ctx.world.foothold(ip):
            return {"stage": stage, "effect": "implant_channel", "ok": False,
                    "detail": "light mode needs a foothold to plant the channel"}
        result = execute("implant_channel", {"ip": ip}, ctx, source="seed")
        return {"stage": stage, "effect": "implant_channel", "ok": result.ok,
                "detail": result.detail}
    if stage == "persist":
        result = execute("persist", {"ip": ip, "family": "auto"}, ctx, source="seed")
        return {"stage": stage, "effect": "persist", "ok": result.ok,
                "detail": result.detail}
    return {"stage": stage, "effect": None, "ok": False,
            "detail": f"unknown stage {stage}"}


def _run_evasion(ctx, ip, dry_run=False):
    """One evasion touch per box (the families whose point is blue's hunting cost)."""
    available = _families_for(ctx, ip)
    for family in EVASION_FAMILIES:
        if family not in available:
            continue
        result = execute("persist", {"ip": ip, "family": family}, ctx, source="seed")
        return {"stage": "evasion", "effect": f"persist:{family}", "ok": result.ok,
                "detail": result.detail}
    return None


def _families_for(ctx, ip):
    from .effects.persist import families
    return families(ctx.is_windows(ip))


def write_manifest(ctx, results, *, mode, depth, out_dir=None):
    """`red/manifest.json`: what is planted where, with the restore recipes."""
    comp_dir = Path(ctx.cfg["run"].get("comp_dir") or ctx.cfg["run"]["state_dir"])
    out = Path(out_dir) if out_dir else comp_dir / "red"
    boxes = {}
    for ip, record in results.items():
        artifacts = [{"kind": a.get("kind"), "detail": a.get("detail"),
                      "paths": a.get("paths") or []}
                     for a in ctx.world.artifacts(ip)]
        restores = [entry for entry in ctx.world.restore_table() if entry.get("ip") == ip]
        boxes[ip] = {**{k: v for k, v in record.items() if k != "actions"},
                     "artifacts": artifacts,
                     "restore": restores}
    manifest = {"comp": ctx.world._d.get("comp") if hasattr(ctx.world, "_d") else None,
                "assume_breach": mode, "depth": depth,
                "generated_at": now_iso(), "boxes": boxes}
    try:
        atomic_write(out / "manifest.json", json.dumps(manifest, indent=2), mode=0o600)
        manifest["path"] = str(out / "manifest.json")
    except OSError as exc:                    # read-only comp dir is not fatal
        manifest["path"] = None
        manifest["error"] = str(exc)
    return manifest
