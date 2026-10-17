import contextlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path


# Cookie jars live per-run, not in the shared /tmp (see jar_path).
JAR_DIRNAME = ".jars"


_JAR_LOCKS = {}


# /tmp/jar.<team> was a predictable name in shared /tmp, created by `curl -c` at the
# driver's umask: any local account could read a live session cookie, and a second scrim
# on the same host clobbered the first one's jar. The name must stay deterministic (the
# generated qlogin/score.py helpers and this driver agree on one file), so a fixed
# basename inside a 0700 run-dir subdirectory, seeded via mkstemp+rename so the mode is
# 0600 by construction instead of by luck with the umask.
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


# one re-entrant lock per Quotient account: the loops refresh per-team jars concurrently
# and Quotient allows ONE session per account, so two refreshers invalidate each other's
# cookie in a loop (the documented "three-round scoreboard mystery"). Re-entrant so a
# helper refreshing while already holding the account lock (qget -> _qlogin) cannot
# deadlock against itself.
def jar_lock(account):
    return _JAR_LOCKS.setdefault(account, threading.RLock())


# `'"error"' in body` flipped the verdict on any payload whose DATA contained that text
# (a service or inject legitimately named "error…") and missed an error object written
# with different spacing; a non-JSON body is NOT an error — callers json.loads() it and
# fail loudly on their own
def json_error(body):
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and "error" in data


# curl writes a private temp file renamed over the shared jar: never observed half-written
# (`curl -c` truncates in place, so a concurrent reader could see a partial file), the
# 0600 mode survives, and a FAILED refresh leaves the previous jar intact instead of
# destroying a working session. Callers in the loops hold jar_lock(account); the lock
# here covers the one-off callers too.
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


# without the isinstance guard an error body made next(...) raise StopIteration/TypeError
# from inside the monitor thread, which died silently (a harness audit finding)
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


# Last10Rounds is newest-first and at >=8 teams a poll can land mid-round: rounds[0] is
# then the round in flight, with an empty Checks array that is not a verdict at all —
# reading it as "no check passed" reports a phantom DOWN the soak's monitors, fire-tests
# and scoreboard snapshots would all have believed. Take the newest round that actually
# HAS checks; only when no round carries any is the service genuinely unmeasured, and
# unmeasured stays down (fail closed) rather than being quietly promoted to up.
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
