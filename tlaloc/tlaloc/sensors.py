"""Sensors: the read-only scoreboard mirror (Quotient) and the restore detector.

Two jobs, both read-only (never POST anything but the login that establishes the
scoreboard session):

* **pressure measurement** — Quotient's latest scored round per team/service,
  which is what the policy caps and the digest read;
* **blue's behaviour** — a service that was DOWN and is UP again between two
  polls is a blue restore: the strongest available signal that blue is working
  the box, and the trigger for the ladder's mercy rule and for re-planting.

The sensor is deliberately dumb. It reports; it never decides, never writes to
the scoreboard, and never touches a team box.
"""

import time

from .errors import TlalocError

# How long a scoreboard value is trusted. Quotient polls every ~70s; anything
# older than this is stale and reported as such rather than silently used.
STALE_AFTER_SEC = 300


def _check_passed(check):
    result = check.get("Result")
    if isinstance(result, str):
        return result.strip().lower() not in ("", "0", "false")
    return bool(result)


class QuotientSensor:
    """GET-only client for the scoring engine's API (session cookie + re-login)."""

    def __init__(self, base_url, admin_password, timeout=10):
        self.base_url = (base_url or "").rstrip("/")
        self.admin_password = admin_password
        self.timeout = timeout
        self.session = None
        self.calls = 0

    def _login(self):
        import requests                                     # lazy
        session = requests.Session()
        response = session.post(f"{self.base_url}/api/login",
                                json={"username": "admin",
                                      "password": self.admin_password},
                                timeout=self.timeout)
        response.raise_for_status()
        self.session = session
        return session

    def _get(self, path):
        if self.session is None:
            self._login()
        response = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
        if response.status_code == 401:
            self._login()                                   # cookie expired: once
            response = self.session.get(f"{self.base_url}{path}", timeout=self.timeout)
        response.raise_for_status()
        self.calls += 1
        return response.json()

    def teams(self):
        return [{"id": t["ID"], "name": t.get("Name", t.get("Identifier"))}
                for t in self._get("/api/teams")]

    def team_service_map(self):
        """{team_name: {ServiceName: bool}} from the latest scored round."""
        out = {}
        for team in self.teams():
            services = self._get(f"/api/services/{team['id']}") or []
            status = {}
            for service in services:
                name = service.get("ServiceName", "?")
                rounds = service.get("Last10Rounds") or []
                first = rounds[0] if rounds else None
                checks = (first.get("Checks") if isinstance(first, dict) else None) or []
                if checks:               # unscored services are simply absent
                    status[name] = all(_check_passed(c) for c in checks)
            out[team["name"]] = status
        return out


class Sensors:
    """The loop's view: poll, compare with the last poll, report what changed."""

    def __init__(self, cfg, intel, world=None, log=None, client=None, now=None):
        sensors_cfg = cfg.get("sensors") or {}
        self.cfg = cfg
        self.intel = intel
        self.world = world
        self.log = log
        self.now = now or time.time
        self.enabled = bool(sensors_cfg.get("quotient_enabled", True))
        self.poll_timeout = float(sensors_cfg.get("poll_timeout_sec", 10))
        self.last_map = {}
        self.last_poll_at = None
        self.last_error = None
        self._client = client
        self.polls = 0

    # -- client -------------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            url = (self.cfg["sensors"].get("quotient_url")
                   or self.intel.discover_engine_ip())
            password = self.intel.quotient_admin_password()
            if not url or not password:
                self.enabled = False
                self.last_error = "no scoreboard URL/password (blind mode?)"
                return None
            self._client = QuotientSensor(url, password, timeout=self.poll_timeout)
        return self._client

    # -- polling ------------------------------------------------------------

    def poll(self):
        """{team: {service: up}} or None when the scoreboard is unusable."""
        if not self.enabled:
            return None
        client = self.client
        if client is None:
            return None
        try:
            mapping = client.team_service_map()
        except TlalocError:
            raise
        except Exception as exc:                 # network/JSON/HTTP
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self.log is not None:
                self.log.emit("sensor_error", source="quotient", error=self.last_error)
            return None
        self.polls += 1
        self.last_error = None
        return mapping

    def observe(self):
        """One cycle's read: returns (service_map, down_counts, restores).

        `restores` is a list of {team, service} that went DOWN -> UP since the
        previous poll, recorded into world state as blue events (which is what
        the ladder's mercy rule and the digest's `resp` section read).
        """
        mapping = self.poll()
        if mapping is None:
            return self.last_map, self.counts(), []
        restores = diff_restores(self.last_map, mapping)
        for event in restores:
            if self.world is not None:
                self.world.add_blue_event(event["team"], event["service"])
        if self.log is not None and restores:
            self.log.emit("blue_restores", restores=restores, cycle_poll=self.polls)
        self.last_map = mapping
        self.last_poll_at = self.now()
        return mapping, self.counts(), restores

    def team_service_map(self):
        return dict(self.last_map)

    def counts(self):
        """{team: (up, down)} — the pressure gauge the caps read."""
        out = {}
        for team, status in (self.last_map or {}).items():
            up = sum(1 for ok in status.values() if ok)
            out[team] = (up, len(status) - up)
        return out

    def fresh(self):
        if self.last_poll_at is None:
            return False
        return (self.now() - self.last_poll_at) <= STALE_AFTER_SEC

    def status(self):
        return {"enabled": self.enabled, "polls": self.polls,
                "fresh": self.fresh(), "error": self.last_error,
                "teams": len(self.last_map)}


def diff_restores(previous, current):
    """Services that flipped DOWN -> UP between two polls (blue's work).

    A service absent from either side is not a flip: the map only carries scored
    services, and a service that disappears is a scoring change, not a restore.
    """
    out = []
    for team, status in (current or {}).items():
        before = (previous or {}).get(team) or {}
        for service, is_up in status.items():
            if service in before and is_up and not before[service]:
                out.append({"team": team, "service": service})
    return out
