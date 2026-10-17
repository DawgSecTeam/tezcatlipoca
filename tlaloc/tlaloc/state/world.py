"""World v2: the agent's belief state, one schema-versioned JSON document.

Changes from v1 (each kills an audited defect):

* schema `v` field + comp stamp — a state file written for another competition
  (or another schema) is REFUSED at load. v1's --reset-event leaked tallies,
  burndown rows and seed_done across events; here the fresh-dir-per-comp
  contract is enforced by the loader, not by operator memory.
* restore table (`restores`) is first-class: every recoverable effect appends
  its undo recipe here, and the verdict/organizer artifact is a projection of
  it (principle 6). v1 scattered restore knowledge through notes and three
  hardcoded unrecoverable-mode lists.
* `ensure_event_start` saves ONLY when it creates the key (v1 write-amplified
  every elapsed_min() call).
* c2 section: callback freshness per box (implant success is "the callback is
  in the operation", never process liveness).

Concurrency contract is unchanged from v1 and deliberate: flock on
world.json.lock (EX save / SH reload), reload-then-mutate-then-save, every
mutator saves the whole file, no merge. The loop reloads at the top of every
cycle — disk is truth.
"""

import fcntl
import json
import re
import time
from pathlib import Path

from ..errors import StateError
from .schema import SCHEMA, atomic_write, now_iso, parse_ts

# Unit name or beacon id inside an artifact's detail — the discriminator that
# makes a per-instance key (one box can carry several implants of one kind).
_UNIT_RE = re.compile(r"\(([\w.@-]+\.(?:service|timer|task))\)")
_ID_RE = re.compile(r"id=(\S+)")


def artifact_key(artifact):
    detail = artifact.get("detail") or ""
    m = _UNIT_RE.search(detail) or _ID_RE.search(detail)
    return f"{artifact.get('ip')}|{artifact.get('kind')}|{m.group(1) if m else ''}"


def _blank(comp):
    return {"schema": SCHEMA, "comp": comp or "", "meta": {}, "targets": {},
            "creds": [], "footholds": [], "artifacts": [], "impacts": [],
            "restores": [], "sabotage": [], "blue_events": [], "c2": {}}


class World:
    def __init__(self, state_dir, comp=None):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "world.json"
        self.comp = comp or ""
        if not self.reload():
            self._d = _blank(comp)
            self.save()

    # -- persistence ---------------------------------------------------------

    @property
    def lock_path(self):
        return Path(str(self.path) + ".lock")

    def _locked(self, mode):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "w")
        fcntl.flock(fh, mode)
        return fh

    def reload(self):
        if not self.path.exists():
            return False
        lock = self._locked(fcntl.LOCK_SH)
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        if data.get("schema") != SCHEMA:
            raise StateError(
                f"{self.path} is schema {data.get('schema')!r}, this build "
                f"writes schema {SCHEMA}: use a fresh state dir per competition")
        stored = data.get("comp") or ""
        if self.comp and stored and stored != self.comp:
            raise StateError(
                f"{self.path} belongs to comp {stored!r}, not {self.comp!r} — "
                "cross-event state is the leak v1 shipped; fresh dir per comp")
        self._d = data
        return True

    def save(self):
        lock = self._locked(fcntl.LOCK_EX)
        try:
            atomic_write(self.path, json.dumps(self._d, indent=2))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    # -- meta / event clock ----------------------------------------------------

    @property
    def meta(self):
        return self._d["meta"]

    def ensure_event_start(self):
        if "event_start" not in self.meta:
            self.meta["event_start"] = time.time()
            self.save()          # only on create (v1 saved on every read)
        return self.meta["event_start"]

    # -- targets ---------------------------------------------------------------

    def target(self, ip):
        return self._d["targets"].setdefault(
            ip, {"os": None, "team": None, "ports": [], "recon_done": False,
                 "last_recon_at": None, "creds_tried": {}, "notes": [],
                 "fails": {}})

    def target_ips(self):
        return sorted(self._d["targets"])

    def target_record(self, ip):
        return self._d["targets"].get(ip)

    def note_recon(self, ip, ports, os_guess=None):
        t = self.target(ip)
        t["ports"] = ports
        t["recon_done"] = True
        t["last_recon_at"] = now_iso()
        if os_guess:
            t["os"] = os_guess
        self.save()

    def note(self, ip, text):
        self.target(ip).setdefault("notes", []).append(f"{now_iso()} {text}")
        self.save()

    def creds_tried(self, ip, user):
        return self.target(ip).get("creds_tried", {}).get(user, 0)

    def count_cred_try(self, ip, user):
        t = self.target(ip)
        t.setdefault("creds_tried", {})
        t["creds_tried"][user] = t["creds_tried"].get(user, 0) + 1
        self.save()

    # -- credentials / footholds ----------------------------------------------

    def add_cred(self, user, password, source, ip=None, key_path=None):
        """key_path marks a private-key credential (password may be empty);
        consumers must prefer key_path when present."""
        for cred in self._d["creds"]:
            if cred["user"] == user and cred.get("key_path", password) in (password, key_path):
                if ip and ip not in cred["valid_on"]:
                    cred["valid_on"].append(ip)
                break
        else:
            entry = {"user": user, "password": password or "", "source": source,
                     "valid_on": [ip] if ip else []}
            if key_path:
                entry["key_path"] = key_path
            self._d["creds"].append(entry)
        self.save()

    def valid_creds(self, ip=None):
        if ip:
            return [c for c in self._d["creds"] if ip in c["valid_on"]]
        return list(self._d["creds"])

    def forget_creds(self, user, source=None):
        self._d["creds"] = [c for c in self._d["creds"]
                            if not (c["user"] == user
                                    and (source is None or c.get("source") == source))]
        self.save()

    def add_foothold(self, ip, user, password, kind, windows=False,
                     key_path=None, escalation=None):
        """One foothold per IP (update in place). Escalation survives only for
        the SAME user — a re-drop with different creds lands a user foothold
        with escalation=None (v1 silently kept the stale root label)."""
        existing = self.foothold(ip)
        if existing and existing["user"] != user:
            escalation = None
            existing["escalation"] = None
        payload = {"user": user, "password": password, "kind": kind,
                   "windows": windows, "root_capable": kind == "root",
                   "last_ok": now_iso()}
        if escalation is not None:
            payload["escalation"] = escalation
        if key_path:
            payload["key_path"] = key_path
        if existing:
            existing.update(payload)
            self.save()
            return existing
        rec = {"ip": ip, "escalation": escalation, "since": now_iso(), **payload}
        self._d["footholds"].append(rec)
        self.save()
        return rec

    def elevate_foothold(self, ip, escalation):
        fh = self.foothold(ip)
        if fh is None:
            return None
        fh.update({"kind": "root", "root_capable": True,
                   "escalation": escalation, "last_ok": now_iso()})
        self.save()
        return fh

    def foothold(self, ip):
        return next((f for f in self._d["footholds"] if f["ip"] == ip), None)

    def footholds(self):
        return list(self._d["footholds"])

    def drop_foothold(self, ip):
        self._d["footholds"] = [f for f in self._d["footholds"] if f["ip"] != ip]
        self.save()

    def touch_foothold(self, ip):
        fh = self.foothold(ip)
        if fh:
            fh["last_ok"] = now_iso()
            self.save()

    # -- artifacts ---------------------------------------------------------------

    def add_artifact(self, ip, kind, detail, check_cmd=None, paths=None, **tags):
        """One artifact row. The five named fields are the schema; `**tags`
        carries the effect's own identifying fields — `persist` tags every row
        with its `family` and the re-plant logic reads it back (policy._family_of),
        so a row that cannot carry the tag makes every persist plant unrecordable."""
        rec = {"ip": ip, "kind": kind, "detail": detail, "check_cmd": check_cmd,
               "paths": list(paths or []), "planted_at": now_iso(),
               "last_ok": now_iso()}
        rec.update(tags)
        self._d["artifacts"].append(rec)
        self.save()
        return len(self._d["artifacts"]) - 1

    def artifacts(self, ip=None):
        if ip:
            return [a for a in self._d["artifacts"] if a["ip"] == ip]
        return list(self._d["artifacts"])

    def artifact_ok(self, idx):
        self._d["artifacts"][idx]["last_ok"] = now_iso()
        self.save()

    def dead_artifacts(self):
        return set(self.meta.get("dead_artifacts") or [])

    def set_dead_artifacts(self, keys):
        self.meta["dead_artifacts"] = sorted(set(keys))
        self.save()

    # -- impacts -----------------------------------------------------------------

    def add_impact(self, ip, service, mode, detail=None):
        rec = {"ip": ip, "service": service, "mode": mode, "at": now_iso()}
        if detail:
            rec["detail"] = detail
        self._d["impacts"].append(rec)
        self.save()

    def impacts(self):
        return list(self._d["impacts"])

    # -- restore table (principle 6) ------------------------------------------

    def add_restore(self, effect, ip, service, recipe, secrets=None):
        """One undo record. `recipe` is a string or list of steps blue-side or
        organizer-side; `secrets` (e.g. the original password of a rotated
        account) is kept for the 0600 organizer artifact only, never rendered
        into the secrets-free report."""
        rec = {"effect": effect, "ip": ip, "service": service,
               "at": now_iso(), "recipe": recipe}
        if secrets:
            rec["secrets"] = secrets
        self._d["restores"].append(rec)
        self.save()

    def restore_table(self):
        return list(self._d["restores"])

    # -- credlist rotations (organizer restore-only) ---------------------------

    def add_sabotage(self, ip, user, old_password, new_password):
        self._d["sabotage"].append({"ip": ip, "user": user,
                                    "old_password": old_password,
                                    "new_password": new_password,
                                    "at": now_iso()})
        self.save()

    def sabotage(self):
        return list(self._d["sabotage"])

    # -- blue restores (scoreboard DOWN->UP) ------------------------------------

    def add_blue_event(self, team, service, ip=None):
        self._d["blue_events"].append({"team": team, "service": service,
                                       "ip": ip, "at": now_iso()})
        if len(self._d["blue_events"]) > 512:      # bounded, oldest dropped
            self._d["blue_events"] = self._d["blue_events"][-512:]
        self.save()

    def blue_events(self, n=8):
        return list(self._d.get("blue_events", []))[-n:]

    def restore_count(self, team, service, within_min=None, now=None):
        out = 0
        for e in self._d.get("blue_events", []):
            if e["team"] != team or e["service"] != service:
                continue
            if within_min is not None:
                age = (now or time.time()) - parse_ts(e["at"])
                if age > within_min * 60:
                    continue
            out += 1
        return out

    def restores_within(self, minutes, now=None):
        now = now or time.time()
        return [e for e in self._d.get("blue_events", [])
                if now - parse_ts(e["at"]) <= minutes * 60]

    # -- scoreboard mirror --------------------------------------------------------

    def last_service_map(self):
        return self.meta.get("last_service_map") or {}

    def set_last_service_map(self, mapping):
        self.meta["last_service_map"] = mapping
        self.save()

    # -- c2 callback mirror ---------------------------------------------------------

    def note_callback(self, box, seen_at, agent=None, external_id=None):
        self._d["c2"][box] = {"seen_at": seen_at, "agent": agent,
                              "external_id": external_id}
        self.save()

    def callback_age(self, box, now=None):
        rec = self._d["c2"].get(box)
        if not rec:
            return None
        return (now or time.time()) - parse_ts(rec.get("seen_at", ""))

    def callbacks(self):
        return dict(self._d["c2"])

    # -- tallies ------------------------------------------------------------------

    def tally_failure(self, effect, ip):
        if not ip:
            return
        fails = self.target(ip).setdefault("fails", {})
        fails[effect] = fails.get(effect, 0) + 1
        self.save()

    def failure_count(self, effect, ip):
        return (self._d["targets"].get(ip, {}).get("fails", {}) or {}).get(effect, 0)

    def tally_reject(self, effect, team, reason):
        mem = self.meta.setdefault("reject_memory", {})
        entry = mem.setdefault(f"{effect}|{team or '?'}", {"count": 0})
        entry["count"] += 1
        entry["reason"] = reason
        entry["at"] = now_iso()
        self.save()

    def reject_memory(self):
        return dict(self.meta.get("reject_memory") or {})

    # -- seed bookkeeping -----------------------------------------------------------

    def seed_record(self, ip):
        return (self.meta.get("seed") or {}).get(ip)

    def set_seed_record(self, ip, rec):
        self.meta.setdefault("seed", {})[ip] = rec
        self.save()

    def seed_done(self):
        return bool(self.meta.get("seed_done"))

    def set_seed_done(self, value=True):
        self.meta["seed_done"] = bool(value)
        self.save()

    def summary(self):
        return {"comp": self._d.get("comp"),
                "targets": len(self._d["targets"]),
                "creds": len(self._d["creds"]),
                "footholds": len(self._d["footholds"]),
                "artifacts": len(self._d["artifacts"]),
                "impacts": len(self._d["impacts"]),
                "restores": len(self._d["restores"])}
