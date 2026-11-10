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


def _fmt_ts(ts):
    if ts is None:
        return "?"
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%SZ")
    except ValueError:
        return str(ts)


def decision_trace(state_dir, limit=None):
    """CLEF exchanges from the event log; limit=None keeps every cycle."""
    path = Path(state_dir) / "events.jsonl"
    if not path.exists():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") == "decision":
                rows.append(event)
    except OSError:
        return []
    return rows if limit is None else rows[-limit:]


def action_trace(state_dir):
    path = Path(state_dir) / "events.jsonl"
    if not path.exists():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") == "action":
                rows.append(event)
    except OSError:
        return []
    return rows


def c2_matrix(c2, comp_dir):
    """Every planned implant and whether its own callback name checked in."""
    if not c2 or not comp_dir:
        return []
    from .c2.implant import entry_name, load_manifest
    try:
        manifest = load_manifest(comp_dir)
        callbacks = c2.callbacks_by_name()
    except (OSError, ValueError, AttributeError):
        return []
    rows = []
    for key, entry in (manifest.get("boxes") or {}).items():
        if not isinstance(entry, dict):
            continue
        name = entry_name(entry, entry.get("box") or key.split(":", 1)[0])
        record = callbacks.get(name) or callbacks.get(key)
        if entry.get("plant") is False:
            status = "built only"
        elif record and getattr(c2, "record_fresh", lambda r: True)(record):
            status = "callback"
        elif record:
            status = "stale"
        else:
            status = "NO CALLBACK"
        rows.append({"box": entry.get("box") or key.split(":", 1)[0],
                     "transport": entry.get("transport") or "?",
                     "kind": entry.get("kind") or "session",
                     "plant": entry.get("plant") is not False,
                     "name": name, "status": status,
                     "seen_at": (record or {}).get("seen_at"),
                     "callback": entry.get("callback")})
    return rows


def _box_caption(state):
    boxes = []
    for team in state.get("teams") or []:
        for box in team.get("boxes") or []:
            svc = ", ".join(f"{s.get('name')}:{s.get('status')}"
                            for s in box.get("services") or [])
            methods = box.get("access_methods") or {}
            access = ",".join(f"{k}:{v}" for k, v in methods.items())
            boxes.append(f"{box.get('id')}({box.get('os')}, {box.get('access')}, "
                         f"{access}) [{svc}]")
    return "; ".join(boxes) if boxes else "no boxes"


def _render_c2(lines, rows):
    lines += ["", "## C2 implant matrix (per build name)"]
    if not rows:
        lines.append("- no C2 client/manifest available at report time")
        return
    planted = [r for r in rows if r["plant"]]
    live = [r for r in planted if r["status"] == "callback"]
    lines.append(f"- planted: **{len(live)}/{len(planted)}** checked in; "
                 f"built-only: {len(rows) - len(planted)}")
    survived = [r for r in planted if r["status"] == "callback"]
    lines.append("- survived last round: "
                 + (", ".join(f"{r['box']}:{r['transport']}" for r in survived)
                    if survived else "none"))
    missing = [r for r in planted if r["status"] != "callback"]
    if missing:
        lines.append("- **no callback / not working**: "
                     + ", ".join(f"{r['box']}:{r['transport']} ({r['status']})"
                                 for r in missing))
    for r in rows:
        seen = ""
        if r.get("seen_at") is not None:
            try:
                from datetime import datetime
                seen = " last=" + _fmt_ts(
                    datetime.utcfromtimestamp(float(r["seen_at"])).isoformat())
            except (ValueError, TypeError):
                seen = f" last={r['seen_at']}"
        lines.append(f"- {r['box']:<14} {r['transport']:<11} {r['kind']:<8} "
                     f"{r['status']:<12}{seen}  `{r['callback'] or ''}`")
    beacons = [r for r in rows if r["kind"] == "beacon"]
    if beacons:
        called = [r for r in beacons if r["status"] == "callback"]
        lines.append(f"- beacon builds: {len(called)}/{len(beacons)} checked in "
                     "(alternate control paths are built, not seeded)")


def _render_decisions(lines, trace, legacy=None):
    lines += ["", "## CLEF decisions (input → answer → pick)"]
    outcomes = {r.get("cycle"): r.get("outcomes") for r in (legacy or [])}
    if not trace:
        if legacy:
            for record in legacy[-10:]:
                lines.append(f"- cycle {record.get('cycle')}: {record.get('kind')} "
                             f"{record.get('chosen') or record.get('effect') or ''} "
                             f"source={record.get('source')}")
        else:
            lines.append("- no decision events at report time")
        return
    for event in trace:
        cycle, question = event.get("cycle"), event.get("question")
        answer = event.get("answer")
        top = float(event.get("top_prob") or 0)
        theta = float(event.get("theta") or 0)
        source = event.get("source") or "?"
        lines.append(f"### {_fmt_ts(event.get('ts'))} · cycle {cycle} — {question} → "
                     f"`{answer}` ({source}, p={top:.2f}, theta={theta:.2f})")
        bundle = event.get("bundle") or {}
        state = bundle.get("state") or {}
        if state:
            lines.append(f"- given: minute {state.get('minute', '?')}/"
                         f"{state.get('total_minutes', '?')} "
                         f"left {state.get('minutes_left', '?')} "
                         f"phase={state.get('phase', '?')} "
                         f"priority={(state.get('priority_team') or {}).get('name', '?')}; "
                         f"{_box_caption(state)}")
        questions = (bundle.get("questions") or {}).get(question) or {}
        if questions.get("instructions"):
            lines.append(f"- asked: {questions['instructions']}")
        criteria = questions.get("criteria") or {}
        probs = event.get("probabilities") or {}
        if criteria and isinstance(criteria, dict):
            ranked = sorted(probs.items(), key=lambda item: item[1], reverse=True)
            for option, prob in ranked[:6]:
                lines.append(f"  - {prob:.2f} `{option}`: "
                             f"{criteria.get(option, '')[:160]}")
        for outcome in outcomes.get(cycle) or []:
            lines.append(f"- executed `{outcome['id']}`: "
                         f"{'ok' if outcome.get('ok') else 'FAILED'}"
                         + (f" — {outcome['detail']}" if outcome.get("detail") else ""))
        note = event.get("note")
        if note:
            lines.append(f"- note: {note}")
        lines.append("")

def _render_actions(lines, actions):
    lines += ["", "## Cycle actions (every round)"]
    if not actions:
        lines.append("- no action events at report time")
        return
    current = None
    for event in actions:
        cycle = event.get("cycle")
        if cycle != current:
            lines.append(f"### cycle {cycle}")
            current = cycle
        lines.append(f"- {_fmt_ts(event.get('ts'))} `{event.get('effect')}` "
                     f"{json.dumps(event.get('params') or {}, separators=(',', ':'))} "
                     f"→ {'ok' if event.get('ok') else 'FAILED'}: "
                     f"{(event.get('detail') or '')[:400]}")


def render_report(world, *, policy=None, sensors=None, narrative=None,
                  decisions=None, summary=None, trace=None, c2_rows=None,
                  actions=None):
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
        f"- C2 callbacks recorded (per box): **{summary['c2_callbacks']}**",
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
    _render_c2(lines, c2_rows or [])
    _render_actions(lines, actions or [])
    _render_decisions(lines, trace or [], decisions)
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
                    decisions=None, out_dir=None, c2=None, comp_dir=None):
    out = Path(out_dir or state_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(world, policy, sensors)
    report = render_report(world, policy=policy, sensors=sensors,
                           narrative=narrative, decisions=decisions, summary=summary,
                           trace=decision_trace(state_dir),
                           actions=action_trace(state_dir),
                           c2_rows=c2_matrix(c2, comp_dir))
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
