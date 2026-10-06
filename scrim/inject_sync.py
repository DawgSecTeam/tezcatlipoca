import calendar
import json
import subprocess
import time
from pathlib import Path

from scrim import quotient_api
from scrim.core import log


def _inject_reanchor_plan(comp_injects, remote_injects):
    """Match comp injects to engine injects by title; return (updates, missing_titles).

    Each update is (engine_id, form_fields, keep_files) with times already RFC3339.
    Matching mirrors create_injects' title dedup — title is the join key.
    """
    by_title = {}
    for r in remote_injects or []:
        title = r.get("Title")
        if title:
            by_title.setdefault(title, r)
    updates, missing = [], []
    for inj in comp_injects:
        r = by_title.get(inj["title"])
        if r is None:
            missing.append(inj["title"])
            continue
        updates.append((str(r["ID"]),
                        {"open-time": inj["open_time"],
                         "due-time": inj["due_time"],
                         "close-time": inj["close_time"]},
                        list(r.get("InjectFileNames") or [])))
    return updates, missing


def reanchor_injects(args, comp, creds):
    """Re-anchor the engine's inject clocks at T0 (idempotent; run right before stage_run).

    Inject offsets resolve to absolute times at phase-7 deploy time, so a --skip-deploy
    restart or a long staging gap reaches T0 with every inject already expired
    (shakedown-5x4: blue inject score was a guaranteed 0). UpdateInject deletes any
    attachment NOT listed under keep-files, so every existing InjectFileNames entry is
    re-listed on each POST.
    """
    from config_ops import load_injects, resolve_inject_times

    injects = load_injects(comp)
    if not injects:
        return
    for inj in injects:
        if not (inj["open_offset_min"] <= inj["due_offset_min"] <= inj["close_offset_min"]):
            raise SystemExit(
                f"  ERROR: inject {inj['title']!r} offsets are unordered — need "
                f"open {inj['open_offset_min']} <= due {inj['due_offset_min']} <= close "
                f"{inj['close_offset_min']}; fix competitions/{comp.name}/injects/.")
    resolve_inject_times(injects)
    jar = str(Path(args.run_dir) / ".jar-reanchor")
    quotient_api._qlogin(creds, "admin", jar)
    r = quotient_api.qget(creds, "admin", jar, "/api/injects")
    try:
        remote = json.loads(r.stdout)
        if not isinstance(remote, list):
            raise ValueError(str(remote)[:80])
    except Exception:
        log(f"WARNING: inject re-anchor skipped — /api/injects unreadable: {(r.stdout or '')[:120]}")
        return
    updates, missing = _inject_reanchor_plan(injects, remote)
    for title in missing:
        log(f"WARNING: inject {title!r} has no engine counterpart — deploy phase 7 never created it?")
    n_ok = 0
    for inject_id, fields, keep_files in updates:
        cmd = ["curl", "-s", "--max-time", "15", "-b", jar, "-X", "POST",
               f"http://{creds['ENGINE_IP']}/api/injects/{inject_id}"]
        for key, value in fields.items():
            cmd += ["-F", f"{key}={value}"]
        for fn in keep_files:
            cmd += ["-F", f"keep-files={fn}"]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout or ""
        if quotient_api.json_error(out):
            log(f"WARNING: re-anchoring inject {inject_id} failed: {out[:120]}")
        else:
            n_ok += 1
    if updates:
        log(f"inject clocks re-anchored at T0 ({n_ok}/{len(updates)} updated, "
            f"first opens {updates[0][1]['open-time']})")
    else:
        log("no engine injects matched competitions/%s/injects/ — nothing re-anchored" % comp.name)


def inject_brief(creds, team):
    """One line per inject with submission state; flags due-within-30-min as task #1."""
    jar = quotient_api.jar_path(creds["RUN_DIR"], team)
    try:
        r = quotient_api.qget(creds, team, jar, "/api/injects")
        injects = json.loads(r.stdout)
        if not isinstance(injects, list):
            raise ValueError(f"unexpected payload: {str(injects)[:80]}")
    except Exception as e:
        return f"(inject list unavailable: {type(e).__name__}: {e})"
    if not injects:
        return "(no injects published yet)"
    now = time.time()
    lines = []

    def due_in_min(due):
        try:
            naive = time.strptime(due[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
        for cand in (calendar.timegm(naive) - now, time.mktime(naive) - now):
            if -15 * 60 <= cand <= 12 * 3600:
                return cand / 60
        return None

    for i in sorted(injects, key=lambda x: x.get("DueTime") or ""):
        subs = len(i.get("Submissions") or [])
        due_in = due_in_min(i.get("DueTime") or "")
        when = f"due in {due_in:.0f}min" if due_in is not None else f"due {(i.get('DueTime') or '')[11:16]}"
        state = "submitted ✓" if subs else "OPEN"
        urgent = (not subs) and due_in is not None and 0 <= due_in <= 30
        lines.append(f"  #{i.get('ID')} {i.get('Title')} — {when} — {state}"
                     + ("  << TASK #1: submit before close" if urgent else ""))
    return "\n".join(lines)
