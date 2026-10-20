"""Reports and the teardown contract."""

import json
from pathlib import Path

from .state.schema import atomic_write, now_iso


def summarize(world, policy=None, sensors=None):
    impacts = world.impacts()
    artifacts = world.artifacts()
    footholds = world.footholds()
    return {
        "comp": (world._d.get("comp") if hasattr(world, "_d") else None),
        "generated_at": now_iso(),
        "elapsed_min": round(policy.elapsed_min(), 1) if policy else None,
        "phase": policy.event_phase() if policy else None,
        "targets": len(world.target_ips()),
        "footholds": len(footholds),
        "windows_footholds": len([f for f in footholds if f.get("windows")]),
        "root_paths": len([f for f in footholds
                           if f.get("kind") == "root" or f.get("root_capable")]),
        "creds": len(world.valid_creds()),
        "artifacts": len(artifacts),
        "artifacts_live": len([a for a in artifacts
                               if _artifact_key(a) not in world.dead_artifacts()]),
        "artifacts_dead": len(world.dead_artifacts()),
        "impacts": len(impacts),
        "impacts_by_team": _by_team(world, impacts),
        "restores": len(world.restore_table()),
        "blue_restores": len(world.blue_events(500)),
        "sabotage": len(world.sabotage()),
        "seeded": bool(world.seed_done()),
        "seed_boxes": len(world.meta.get("seed") or {}),
        "c2_callbacks": len(world.callbacks()),
        "sensor_polls": getattr(sensors, "polls", None),
    }


def _artifact_key(artifact):
    from .state.world import artifact_key
    return artifact_key(artifact)


def _by_team(world, impacts):
    out = {}
    for impact in impacts:
        record = world.target_record(impact.get("ip") or "") or {}
        key = impact.get("team") or record.get("team") or "?"
        out[key] = out.get(key, 0) + 1
    return out


def restore_table(world):
    """Undo-table projection; the engine's red-verdict and destroy consume this."""
    return [
        {"effect": entry.get("effect"), "ip": entry.get("ip"),
         "service": entry.get("service"), "at": entry.get("at"),
         "recipe": entry.get("recipe"),
         "has_secrets": bool(entry.get("secrets"))}
        for entry in world.restore_table()
    ]


def restore_commands(world):
    out = []
    for entry in world.restore_table():
        recipe = entry.get("recipe")
        steps = recipe if isinstance(recipe, list) else [recipe]
        for step in steps:
            if step:
                out.append({"ip": entry.get("ip"), "effect": entry.get("effect"),
                            "command": str(step)})
    return out


def render_report(world, *, policy=None, sensors=None, narrative=None,
                  decisions=None, summary=None):
    summary = summary or summarize(world, policy, sensors)
    lines = [f"# tlaloc after-action — {summary.get('comp') or 'unset competition'}",
             "",
             f"Generated {summary['generated_at']} (event clock "
             f"{summary.get('elapsed_min')} min, phase {summary.get('phase')}).",
             ""]
    if narrative:
        lines += [narrative.strip(), ""]
    lines += [
        "## Pressure",
        f"- Impacts recorded: **{summary['impacts']}** "
        f"({', '.join(f'{k}: {v}' for k, v in summary['impacts_by_team'].items()) or 'none'})",
        f"- Blue restores seen: **{summary['blue_restores']}**",
        f"- Credential rotations: **{summary['sabotage']}**",
        "",
        "## Access and depth",
        f"- Footholds: **{summary['footholds']}** "
        f"(root/admin paths: {summary['root_paths']}, Windows: {summary['windows_footholds']})",
        f"- Staged/planted artifacts: **{summary['artifacts']}** "
        f"(live {summary['artifacts_live']}, dead {summary['artifacts_dead']})",
        f"- C2 callbacks recorded: **{summary['c2_callbacks']}**",
        f"- Seeded: {summary['seeded']} ({summary['seed_boxes']} boxes)",
        "",
        "## Restore table (blue-side, secrets-free)",
    ]
    table = restore_table(world)
    if not table:
        lines.append("- nothing to restore (no recoverable mutation was made)")
    for entry in table:
        steps = entry["recipe"] if isinstance(entry["recipe"], list) else [entry["recipe"]]
        lines.append(f"- `{entry['effect']}` on {entry['ip']} "
                     f"{('[' + entry['service'] + ']') if entry['service'] else ''}: "
                     + " ; ".join(str(s) for s in steps if s))
    if decisions:
        lines += ["", "## Decision log (last cycles)"]
        for record in decisions[-25:]:
            lines.append(f"- cycle {record.get('cycle')}: {record.get('kind')} "
                         f"{record.get('chosen') or record.get('effect') or ''} "
                         f"source={record.get('source')} "
                         f"top_prob={record.get('top_prob', '-')}")
    lines.append("")
    return "\n".join(lines)


def render_secrets(world, *, summary=None):
    summary = summary or summarize(world)
    lines = [f"# tlaloc restore secrets — {summary.get('comp') or 'unset competition'}",
             "",
             "Organizer-facing. Contains credentials changed during the event; "
             "keep it off the blue side of the table.",
             "",
             "## Rotated credentials"]
    sabotage = world.sabotage()
    if not sabotage:
        lines.append("- none")
    for entry in sabotage:
        lines.append(f"- {entry['ip']} {entry['user']}: "
                     f"`{entry['new_password']}` → restore to `{entry['old_password']}`")
    lines += ["", "## Restore steps carrying secrets"]
    hits = [e for e in world.restore_table() if e.get("secrets")]
    if not hits:
        lines.append("- none")
    for entry in hits:
        lines.append(f"- {entry['effect']} on {entry['ip']}: {entry['secrets']}")
    lines.append("")
    return "\n".join(lines)


def write_artifacts(state_dir, world, *, policy=None, sensors=None, narrative=None,
                    decisions=None, out_dir=None):
    out = Path(out_dir or state_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(world, policy, sensors)
    report = render_report(world, policy=policy, sensors=sensors,
                           narrative=narrative, decisions=decisions, summary=summary)
    secrets = render_secrets(world, summary=summary)
    table = {"comp": summary.get("comp"), "generated_at": now_iso(),
             "entries": restore_table(world),
             "commands": restore_commands(world),
             "summary": summary}
    paths = {
        "report": out / "report.md",
        "secrets": out / "report-secrets.md",
        "restore_table": out / "restore-table.json",
    }
    atomic_write(paths["report"], report, mode=0o644)
    atomic_write(paths["secrets"], secrets, mode=0o600)
    atomic_write(paths["restore_table"], json.dumps(table, indent=2), mode=0o600)
    return {name: str(path) for name, path in paths.items()}
