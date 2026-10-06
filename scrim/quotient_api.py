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


def jar_dir(run_dir):
    """The run's private cookie-jar directory (0700, never shared /tmp)."""
    d = Path(run_dir) / JAR_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def jar_path(run_dir, account):
    """Deterministic per-account cookie jar inside the run dir, created 0600.

    `/tmp/jar.<team>` was a predictable name in the shared /tmp, created by `curl -c` at
    whatever umask the driver had: any local account on the operator host could read a
    live Quotient session cookie, and a second scrim on the same host clobbered the first
    one's jar. The name must stay deterministic (the generated qlogin/score.py helpers
    and this driver have to agree on one file), so it keeps a fixed basename — but inside
    a 0700 run-dir subdirectory, and seeded via mkstemp+rename so the mode is 0600 by
    construction instead of by luck with the umask.
    """
    path = jar_dir(run_dir) / f"{account}.jar"
    if not path.exists():
        fd, tmp = tempfile.mkstemp(prefix=f".{account}-", dir=str(path.parent))
        os.close(fd)
        os.replace(tmp, path)
    return str(path)


def jar_lock(account):
    """One re-entrant lock per Quotient account.

    monitor_loop, inject_brief and the watchdog refresh the per-team jars concurrently,
    and Quotient allows ONE session per account: two refreshers invalidate each other's
    cookie in a loop — the documented cause of the "three-round scoreboard mystery".
    Re-entrant so a helper that refreshes while already holding the account lock (qget ->
    _qlogin) cannot deadlock against itself.
    """
    return _JAR_LOCKS.setdefault(account, threading.RLock())


def json_error(body):
    """True when a Quotient response is a JSON object carrying an `error` key.

    `'"error"' in body` flipped the verdict on any payload whose DATA contained that text
    (a service or inject legitimately named "error…") and missed an error object written
    with different spacing. A non-JSON body is NOT an error here: callers json.loads() it
    and fail loudly on their own.
    """
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and "error" in data


def _qlogin(creds, user, jar):
    """Refresh a Quotient cookie jar for one account (shared jar; newest login wins).

    curl writes a private temp file which is renamed over the shared jar: the jar is
    never observed half-written (`curl -c` truncates in place, so a concurrent reader
    could see a partial file), the 0600 mode survives, and a FAILED refresh leaves the
    previous jar intact instead of destroying a working session. Callers in the loops
    hold jar_lock(account); the lock here covers the one-off callers too.
    """
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
    """GET a Quotient path with the account's shared jar; re-login and retry once on rejection."""
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


def _team_tid(creds, user, jar, team):
    """Map a team name to Quotient's internal team ID via /api/teams.

    Raises ValueError on a payload that is not a team list. Without the isinstance guard
    an error body made `next(...)` raise StopIteration/TypeError from inside the monitor
    thread, which used to die silently (audit find D3).
    """
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


def team_down(creds, team):
    try:
        return any(not s["up"] for s in parsed_status(creds, team))
    except Exception:
        return False


def services_to_rows(services):
    """Engine /api/services payload -> [{service, up, error}] (parsed_status + final capture).
    A team with no registered/scored services is a legitimate `null` body.

    `Last10Rounds` is ordered newest-first, and at >=8 teams a poll can land mid-round:
    rounds[0] is then the round in flight, with an empty `Checks` array that is not a
    verdict at all. Reading it as "no check passed" reports a phantom DOWN — which the
    soak's monitors, fire-tests and scoreboard snapshots would all have believed. So take
    the newest round that actually HAS checks; only when no round carries any is the
    service genuinely unmeasured, and unmeasured stays down (fail closed) rather than
    being quietly promoted to up."""
    rows = []
    for s in services or []:
        rounds = s.get("Last10Rounds") or []
        checks = next((r.get("Checks") for r in rounds if r.get("Checks")), None) or []
        up = bool(checks) and all(c.get("Result") for c in checks)
        err = next((c.get("Error", "") for c in checks if c.get("Error") and not c.get("Result")), "")
        rows.append({"service": s["ServiceName"], "up": bool(up), "error": err[:120]})
    return rows


def parsed_status(creds, team):
    """Parsed scoreboard for one team: [{service, up, error}]; raises on failure."""
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
    """Compact plain-text scoreboard for the cycle prompt."""
    try:
        return render_status(parsed_status(creds, team))
    except Exception as e:
        return f"scoreboard unreachable ({type(e).__name__}: {e})"
