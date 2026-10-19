"""Sensors: read-only Quotient scoreboard mirror and blue-restore detector; reports, never decides."""

import time

from .errors import TlalocError

# Quotient polls every ~70s; values older than this are reported stale, not silently used.
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
        """Quotient's Last10Rounds are newest-first: rounds[0] is the latest scored round."""
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

    def poll(self):
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
        """DOWN -> UP flips land in world state as blue events: read by the mercy rule and digest resp."""
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
    """Absent from either side is a scoring change, not a restore."""
    out = []
    for team, status in (current or {}).items():
        before = (previous or {}).get(team) or {}
        for service, is_up in status.items():
            if service in before and is_up and not before[service]:
                out.append({"team": team, "service": service})
    return out
