import contextlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path


# Cookie jars live per-run, not in the shared /tmp.
JAR_DIRNAME = ".jars"


_JAR_LOCKS = {}


# Name must stay deterministic (the generated qlogin/score.py helpers agree on one file); the 0700
# run-dir + mkstemp+rename give 0600 by construction, not by umask luck, out of shared-/tmp reach.
def jar_path(run_dir, account):
    d = Path(run_dir) / JAR_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    path = d / f"{account}.jar"
    if not path.exists():
        fd, tmp = tempfile.mkstemp(prefix=f".{account}-", dir=str(path.parent))
        os.close(fd)
        os.replace(tmp, path)
    return str(path)


# Quotient allows ONE session per account: concurrent refreshers clobber each other's cookie.
# RLock because qget -> _qlogin re-enters the same account lock.
def jar_lock(account):
    return _JAR_LOCKS.setdefault(account, threading.RLock())


# A JSON error object, not substring "error": a service or inject may be named error*.
# A non-JSON body is not an error: callers json.loads() it and fail loudly.
def json_error(body):
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and "error" in data


# curl -c truncates the jar in place — partial reads, and a failed refresh wipes a working
# session — so write a temp file and rename over. Loops already hold jar_lock; this covers one-off callers.
def _qlogin(creds, user, jar):
    with jar_lock(user):
        fd, tmp = tempfile.mkstemp(prefix=f".{Path(jar).name}.", dir=str(Path(jar).parent))
        os.close(fd)
        try:
            subprocess.run(["curl", "-s", "--max-time", "15", "-c", tmp, "-X", "POST",
                            f"http://{creds['ENGINE_IP']}/api/login",
                            "-H", "Content-Type: application/json",
                            "-d", json.dumps({"username": user,
                                              "password": creds[user.upper() + "_PW"]})],
                           capture_output=True)
            if os.path.getsize(tmp) > 0:
                os.chmod(tmp, 0o600)
                os.replace(tmp, jar)
                return True
        except OSError:
            pass
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False


def qget(creds, user, jar, path):
    def _get():
        return subprocess.run(["curl", "-s", "--max-time", "15", "-b", jar,
                               f"http://{creds['ENGINE_IP']}{path}"],
                              capture_output=True, text=True)
    with jar_lock(user):
        r = _get()
        if not json_error(r.stdout):
            return r
        _qlogin(creds, user, jar)
        return _get()


# Without the isinstance guard, next(...) raises StopIteration/TypeError in a silently-dead monitor thread.
def _team_tid(creds, user, jar, team):
    r = qget(creds, user, jar, "/api/teams")
    try:
        teams = json.loads(r.stdout)
    except ValueError:
        raise ValueError(f"/api/teams is not JSON: {(r.stdout or '')[:80]!r}")
    if not isinstance(teams, list):
        raise ValueError(f"/api/teams returned {str(teams)[:80]}")
    tid = next((t.get("ID") for t in teams if isinstance(t, dict) and t.get("Name") == team), None)
    if tid is None:
        raise ValueError(f"team {team!r} is not registered in /api/teams")
    return str(tid)


# Last10Rounds is newest-first; a mid-round poll puts an empty-Checks round at [0] — a phantom DOWN.
# Take the newest round with checks; with none, unmeasured stays down (fail closed).
def services_to_rows(services):
    rows = []
    for s in services or []:
        rounds = s.get("Last10Rounds") or []
        checks = next((r.get("Checks") for r in rounds if r.get("Checks")), None) or []
        up = bool(checks) and all(c.get("Result") for c in checks)
        err = next((c.get("Error", "") for c in checks if c.get("Error") and not c.get("Result")), "")
        rows.append({"service": s["ServiceName"], "up": bool(up), "error": err[:120]})
    return rows


def parsed_status(creds, team):
    jar = jar_path(creds["RUN_DIR"], team)
    r = qget(creds, team, jar, f"/api/services/{_team_tid(creds, team, jar, team)}")
    try:
        services = json.loads(r.stdout)
    except Exception:
        raise ValueError(f"non-JSON body: {(r.stdout or '')[:80]!r}")
    if not isinstance(services, list):
        raise ValueError(f"unexpected payload: {str(services)[:80]}")
    return services_to_rows(services)


def render_status(rows):
    return "\n".join(f"{'UP  ' if s['up'] else 'DOWN'} {s['service']:16s} {s['error'][:60]}"
                     for s in rows)


def status_text(creds, team):
    try:
        return render_status(parsed_status(creds, team))
    except Exception as e:
        return f"scoreboard unreachable ({type(e).__name__}: {e})"
