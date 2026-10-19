import calendar
import json
import subprocess
import time
from pathlib import Path

from scrim import quotient_api
from scrim.core import log


# title is the join key, mirroring create_injects' title dedup
def _inject_reanchor_plan(comp_injects, remote_injects):
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


# inject offsets resolve to absolute times at phase-7 deploy time, so a --skip-deploy
# restart or a long staging gap reaches T0 with every inject already expired
# (shakedown-5x4: blue inject score was a guaranteed 0); UpdateInject deletes any
# attachment NOT listed under keep-files, so every existing InjectFileNames entry is
# re-listed on each POST
def reanchor_injects(args, comp, creds):
    from config.injects_config import load_injects, resolve_inject_times

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

    def minutes_until(stamp):
        try:
            naive = time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
        # The engine writes UTC; a naive local timestamp is the only other reading.
        # Taking the first plausible one read an inject 33 minutes overdue as
        # "due in 207min" (the local reading, hours ahead) and hid the miss.
        cands = [c for c in (calendar.timegm(naive) - now, time.mktime(naive) - now)
                 if -12 * 3600 <= c <= 12 * 3600]
        return min(cands, key=abs) / 60 if cands else None

    for i in sorted(injects, key=lambda x: x.get("DueTime") or ""):
        subs = len(i.get("Submissions") or [])
        due_in = minutes_until(i.get("DueTime") or "")
        close_in = minutes_until(i.get("CloseTime") or "")
        closed = close_in is not None and close_in < 0
        if subs:
            state = "submitted ✓"
        elif closed:
            state = "CLOSED (missed)"
        elif due_in is not None and due_in < 0:
            state = "OPEN (overdue)"
        else:
            state = "OPEN"
        if closed:
            when = "closed"
        elif due_in is None:
            when = f"due {(i.get('DueTime') or '')[11:16]}"
        elif due_in < 0:
            when = f"OVERDUE by {-due_in:.0f}min"
        else:
            when = f"due in {due_in:.0f}min"
        urgent = (not subs) and not closed and due_in is not None and due_in <= 30
        lines.append(f"  #{i.get('ID')} {i.get('Title')} — {when} — {state}"
                     + ("  << TASK #1: submit before close" if urgent else ""))
    return "\n".join(lines)
