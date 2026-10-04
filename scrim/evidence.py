import json
import subprocess
from pathlib import Path

from scrim import core
from scrim import procs
from scrim import quotient_api
from scrim import scoreboard_monitor
from scrim.core import log
from scrim.runfiles import SCOREBOARD_STATE, FINAL_SCOREBOARD


def capture_engine_evidence(ev):
    """Copy the engine-side capture into `ev/engine/` (created here); return what was written.

    A copy, never a move: scrim-report.py reads `evidence/final-scoreboard.json` at that
    exact path (scrim_report/loaders.py), so relocating the engine dump would silently zero
    the report's scoreboard section."""
    ev = Path(ev)
    engine_dir = ev / "engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    srcs = [p for p in (ev / FINAL_SCOREBOARD, ev / SCOREBOARD_STATE)
            if p.is_file()]
    srcs += [p for p in sorted(ev.glob("final-services-*.json")) if p.is_file()]
    return [core.secure_evidence(core.shutil_copy(src, engine_dir / src.name)) for src in srcs]


def capture_blue_evidence(run_dir, ev, teams):
    """Seal each blue workdir's deliverables under `ev/blue-team<n>/`; return what was written.

    The submission globs are `sub*.md`/`sub*.txt`, NOT `sub-*.md`: the cycle prompt tells
    blue to write `sub.md`, so the hyphenated pattern matched nothing and observed runs
    counted 0 inject submissions with an empty `submissions/` (plan §3.2). `REPORT.md` is
    blue's after-action report and is copied for the same reason it is asked for."""
    run_dir, ev = Path(run_dir), Path(ev)
    written = []
    for n in range(1, teams + 1):
        src = run_dir / f"blue-team{n}"
        dst = ev / f"blue-team{n}"
        if not src.exists():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("LOG.md", "NOTEBOOK.md", "feed.log", "REPORT.md"):
            if (src / name).exists():
                written.append(core.secure_evidence(core.shutil_copy(src / name, dst / name)))
        for pattern in ("sub*.md", "sub*.txt"):
            for f in src.glob(pattern):
                written.append(core.secure_evidence(core.shutil_copy(f, dst / f.name)))
        for sub in ("submissions", "cycles"):
            if (src / sub).is_dir():
                subprocess.run(["cp", "-r", str(src / sub), str(dst / sub)], check=False)
    log("blue evidence collected")
    return written


def stage_capture(args, creds):
    log("capturing final evidence")
    ev = Path(args.run_dir) / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    admin_jar = str(ev / ".jar-admin")
    teams = scoreboard_monitor._cred_team_names(args, creds)
    # Final scoreboard dump goes to evidence BEFORE anything can tear the engine
    # down — teardown destroys the scoring DB, and the report reads this file.
    final = {"captured_at": core.now_iso(),
             "teams": [], "injects": [], "services": {}}
    for path, key in (("/api/teams", "teams"), ("/api/injects", "injects")):
        r = quotient_api.qget(creds, "admin", admin_jar, path)
        try:
            final[key] = json.loads(r.stdout)
        except Exception:
            log(f"WARNING: final {key} capture failed: {(r.stdout or '')[:120]}")
    for team in teams:
        path = f"/api/services/{quotient_api._team_tid(creds, 'admin', admin_jar, team)}"
        r = quotient_api.qget(creds, "admin", admin_jar, path)
        if quotient_api.json_error(r.stdout):
            r = quotient_api.qget(creds, team, quotient_api.jar_path(args.run_dir, team), path)
        core.write_evidence(ev / f"final-services-{team}.json", r.stdout or "")
        try:
            final["services"][team] = quotient_api.services_to_rows(json.loads(r.stdout))
        except Exception:
            final["services"][team] = None
            log(f"WARNING: {team} services capture failed: {(r.stdout or '')[:120]}")
    core.write_evidence(ev / FINAL_SCOREBOARD, json.dumps(final, indent=1))
    log("final scoreboard dumped to evidence")

    def _pause():
        """POST engine/pause. Returns (ok, detail) — the caller must not claim success.

        The old version ignored both the return code and the body and logged "engine
        paused (best-effort)" unconditionally: a 500 mid-round left scoring running while
        the operator read a line saying it had stopped (audit find D7).
        """
        r = procs.run_tree(["curl", "-s", "--max-time", "15", "-b", admin_jar, "-X", "POST",
                            f"http://{creds['ENGINE_IP']}/api/engine/pause",
                            "-H", "Content-Type: application/json", "-d", '{"pause": true}'],
                           timeout=30, check=False)
        if r.returncode != 0:
            return False, f"curl rc={r.returncode}: {(r.stderr or '').strip()[-120:]}"
        if quotient_api.json_error(r.stdout):
            return False, (r.stdout or "")[:120]
        return True, ""

    # Refresh the admin session before pausing: the capture loop only re-logs-in after a
    # rejected call, so a jar that expired between the last capture and the pause would
    # otherwise silently spend the first attempt on an unauthenticated POST.
    quotient_api._qlogin(creds, "admin", admin_jar)
    paused, why = _pause()
    if not paused:
        quotient_api._qlogin(creds, "admin", admin_jar)
        paused, why = _pause()
    if paused:
        log("engine paused; services JSON captured")
    else:
        log(f"ERROR: engine pause FAILED ({why}) — scoring is still running; pause it by "
            f"hand before assuming the event is over")
    sb = Path(args.run_dir) / SCOREBOARD_STATE
    if sb.exists():
        core.secure_evidence(core.shutil_copy(sb, ev / SCOREBOARD_STATE))
    # The engine capture is also copied into its own subtree (INV8) — a copy, because
    # scrim-report.py reads evidence/final-scoreboard.json where the harness wrote it.
    log(f"engine capture copied to {ev / 'engine'} "
        f"({len(capture_engine_evidence(ev))} file(s))")
    capture_blue_evidence(args.run_dir, ev, args.teams)
