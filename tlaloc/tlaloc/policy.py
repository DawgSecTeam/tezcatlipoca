"""Policy: the ROE as data, the candidate list, and the question bundle."""

import random
import re
import time

from .effects.base import can_root
from .effects.persist import families as persist_families
from .errors import PolicyError
from .state import digest as digest_mod
from .state.world import parse_ts

# Strength order only ("strongest unlocked" + tie-break); the authoritative
# allowlist is the ladder's per-tier `modes`, not this tuple.
MODE_RANK = {
    "recon": -1,
    "stop": 0,
    "stop_mask": 1,
    "stop_rename": 2,
    "stop_break_config": 3,
    "stop_rename_config": 4,
    "block_port": 4,
}

TAKEDOWN_MODES = ("stop", "stop_mask", "stop_rename", "stop_break_config",
                  "stop_rename_config")

BASE_SCORE = {  # small integers so a decision frame stays readable
    "reestablish": 90,
    "implant_channel": 70,
    "escalate": 60,
    "takedown": 55,
    "spray_access": 50,
    "persist": 45,
    "block_port": 40,
    "rotate_credlist": 30,
    "task_channel": 25,
    "lockout": 20,
    "recon": 15,
    "improvise": 10,
}

COST = {"low": 0.0, "med": 1.0, "high": 3.0}

# The gate/arbiter in decide.py treat these differently:
HIGH_STAKES = ("takedown", "rotate_credlist", "lockout", "block_port")


class Candidate(dict):
    @property
    def effect(self):
        return self["effect"]

    @property
    def ip(self):
        return self["params"].get("ip")


def make_candidate(effect, params, score, reason, cost="low", tag=None,
                   high_stakes=None):
    ip = params.get("ip")
    parts = [effect]
    if ip:
        parts.append(str(ip))
    for key in ("service", "port", "user", "family"):
        if params.get(key) is not None:
            parts.append(str(params[key]))
    cid = ":".join(parts)
    cand = Candidate(id=cid, effect=effect, params=dict(params), score=float(score),
                     reason=reason, cost=cost, tag=tag or cid,
                     high_stakes=bool(high_stakes))
    return cand


# Scored services are named `<box>-<short>` by the engine, intel carries package
# names; without one shared key a service taken DOWN reads as up and is re-taken forever.
_SERVICE_ALIASES = {"ldap": "adds", "http": "nginx", "https": "nginx",
                    "apache": "nginx", "httpd": "nginx",   # cde-2026's web01 intel
                    "smtp": "postfix", "imap": "dovecot", "sql": "mysql",
                    "mariadb": "mysql", "mysqld": "mysql",
                    "ftp": "iisftp", "winrm": "enablewinrm"}


def _service_key(name):
    key = re.sub(r"[^a-z0-9]", "", str(name).lower().removesuffix(".service"))
    return _SERVICE_ALIASES.get(key, key)


def _scored_key(name):
    return _service_key(str(name).rsplit("-", 1)[-1])


class Policy:
    def __init__(self, cfg, world, intel, sensors=None, rng=None):
        self.cfg = cfg
        self.world = world
        self.intel = intel
        self.sensors = sensors
        self.rng = rng or random.Random()
        self.red = cfg["red"]
        self.duration_sec = float(self.red["duration_min"]) * 60
        self.counts = {}

    def elapsed_min(self):
        return (self._now() - self.world.ensure_event_start()) / 60.0

    def elapsed_sec(self):
        return self._now() - self.world.ensure_event_start()

    @staticmethod
    def _now():
        import time
        return time.time()

    def progress(self):
        return min(max(self.elapsed_sec() / self.duration_sec, 0.0), 1.0)

    def remaining_sec(self):
        return max(self.duration_sec - self.elapsed_sec(), 0.0)

    def event_over(self):
        return self.remaining_sec() <= 0

    def window_open(self, name):
        minutes = (self.red.get("windows") or {}).get(name)
        if not minutes:
            return False
        return self.remaining_sec() <= float(minutes) * 60

    def teardown_active(self):
        """Final window: the pacing gates suspend and the SAME loop runs full-board takedowns faster."""
        return self.window_open("endgame_teardown")

    def event_phase(self):
        if self.teardown_active():
            return "endgame"
        if self.endgame_active():
            return "final"
        progress = self.progress()
        if progress < 0.2:
            return "opening"
        if progress < 0.6:
            return "build"
        return "surge"

    def endgame_active(self):
        return self.teardown_active() or self.remaining_sec() <= 5 * 60

    def recon_allowed(self):
        return self.remaining_sec() > self.duration_sec * 0.10

    def _restored_recently(self, minutes):
        if not minutes:
            return False
        return bool(self.world.restores_within(float(minutes)))

    def ladder(self):
        """The tier in force now, or None when off. A restore pause shifts the clock back rather than advancing."""
        ladder = self.red.get("ladder") or {}
        if not ladder.get("enabled", True):
            return None
        tiers = ladder.get("tiers") or []
        if not tiers:
            return None
        clock = self.elapsed_min()
        held = False
        pause = float(ladder.get("pause_after_restore_min", 0) or 0)
        if pause and self._restored_recently(pause):
            clock = max(0.0, clock - pause)
            held = True
        pct = min(max(clock / max(self.duration_sec / 60.0, 1.0), 0.0), 1.0)
        tier = tiers[0]
        for candidate in tiers:
            if pct >= float(candidate.get("at_pct", 0)):
                tier = candidate
        out = dict(tier)
        out["held"] = held
        out["at_min"] = round(float(out.get("at_pct", 0)) * (self.duration_sec / 60.0))
        return out

    def next_tier(self):
        tier = self.ladder()
        if tier is None:
            return None
        tiers = (self.red.get("ladder") or {}).get("tiers") or []
        for i, candidate in enumerate(tiers):
            if candidate.get("name") == tier.get("name"):
                return dict(tiers[i + 1]) if i + 1 < len(tiers) else None
        return None

    def allowed_modes(self):
        tier = self.ladder()
        if tier is None:
            return list(MODE_RANK)
        modes = [m for m in (tier.get("modes") or []) if m in MODE_RANK]
        opening = float(self.red.get("opening_takedown_min", 2))
        if "recon" in modes and self.elapsed_min() >= opening:
            modes.append("stop")
        return modes

    def allowed_impact_modes(self):
        """The takedown mechanisms unlocked now — what `takedown.check()` reads."""
        if self.teardown_active():
            return list(TAKEDOWN_MODES)
        return [m for m in self.allowed_modes() if m in TAKEDOWN_MODES]

    def strongest_impact_mode(self):
        modes = self.allowed_impact_modes()
        return max(modes, key=lambda m: MODE_RANK[m]) if modes else None

    def impact_started(self):
        """Red is inside at T0 but takes nothing down until the opening grace is over."""
        if self.teardown_active():
            return True
        tier = self.ladder()
        if tier is not None:
            if bool([m for m in (tier.get("modes") or []) if m in MODE_RANK
                     and m != "recon"]):
                return True
            # observe tier: a short recon window, then the first pressure action
            return self.elapsed_min() >= float(self.red.get("opening_takedown_min", 2))
        return self.elapsed_min() >= float(self.red["caps"].get("grace_min", 10))

    def unrecoverable_unlocked(self):
        if self.teardown_active():
            return True
        tier = self.ladder()
        if tier is not None and tier.get("unrecoverable"):
            return True
        return self.progress() >= 0.5

    def escalation_digest(self):
        tier = self.ladder()
        if tier is None:
            return {"tier": None, "modes": self.allowed_modes()}
        nxt = self.next_tier() or {}
        return {
            "tier": tier.get("name"),
            "modes": self.allowed_modes(),
            "strongest": self.strongest_impact_mode(),
            "boxes_per_team": tier.get("boxes_per_team", 0),
            "held_by_restores": bool(tier.get("held")),
            "next_tier": nxt.get("name"),
            "next_tier_at_min": (round(float(nxt.get("at_pct", 0))
                                       * self.duration_sec / 60.0)
                                 if nxt else None),
        }

    def update_counts(self, counts):
        self.counts = counts or {}

    def max_down_now(self):
        """Concurrent services down per team; 0 means NO LIMIT."""
        caps = self.red.get("caps") or {}
        return max(0, int(caps.get("max_down", 0)))

    def cap_label(self):
        cap = self.max_down_now()
        return "no cap" if cap == 0 else str(cap)

    def can_impact(self, team):
        """The per-team pressure check every impact candidate obeys."""
        if self.teardown_active():
            return True, "final window — the board goes down"
        if not self.impact_started():
            wait = max(0, int(float(self.red["caps"].get("grace_min", 10))
                              - self.elapsed_min()) + 1)
            return False, (f"opening grace window — no service takedowns for another "
                           f"~{wait}min (red already owns the range; blue gets a "
                           f"discovery window)")
        up, down = self.counts.get(team, (None, None))
        if up is None:
            return True, f"{team}: no scoreboard data yet — proceed"
        max_down = self.max_down_now()
        if max_down and down >= max_down:
            return False, f"{team} is at max concurrent down ({down}/{max_down})"
        floor = int((self.red.get("caps") or {}).get("min_standing", 1))
        if up <= floor:
            return False, (f"{team} has only {up} standing services (floor {floor}) "
                           "— ease off")
        return True, "ok"

    def _service_matches(self, a, b):
        a, b = str(a or "").lower(), str(b or "").lower()
        return bool(a and b) and (a in b or b in a)

    def _kill_restore(self, ip, service):
        """Blue restores match at box level: scoreboard names never equal the stopped service's name."""
        kills = [parse_ts(i["at"]) for i in self.world.impacts()
                 if i.get("ip") == ip and self._service_matches(i.get("service"), service)]
        restores = [parse_ts(e["at"]) for e in self.world.blue_events_for(ip=ip)]
        return (max(kills) if kills else None), (max(restores) if restores else None)

    def pressure_rotation(self, ip, service, down, now=None):
        now = now or time.time()
        kill, restore = self._kill_restore(ip, service)
        rotate_min = float((self.red.get("caps") or {}).get("rotate_min", 20))
        if kill is not None and (restore is None or restore < kill):
            return 0.0, "already down since our own takedown — no new damage to score", True
        if kill is not None and restore >= kill:
            return 30.0, (f"blue touched this box {int((now - restore) / 60)}min ago — "
                          "re-hit to test whether they fixed it or just restarted it"), False
        if down and kill is None:
            return 0.0, "the scoreboard already has it down — no new damage to score", True
        recent = [parse_ts(i["at"]) for i in self.world.impacts()
                  if i.get("ip") == ip and now - parse_ts(i["at"]) <= rotate_min * 60]
        penalty = -min(24.0, 6.0 * len(recent))
        return 6.0 + penalty, "not hit yet this event — rotate pressure here", False

    def focus_team(self, rotation_min=None):
        """A shuffle bag, not a re-pick: uniform random starves a team of pressure."""
        teams = [t["team"] for t in self.intel.teams()]
        if not teams:
            raise PolicyError("no teams in intel — nothing to red team")
        meta = self.world.meta
        current = meta.get("focus_team")
        since = meta.get("focus_since") or 0
        rotate_after = float(rotation_min if rotation_min is not None else
                             self.cfg["run"].get("focus_rotation_min", 12)) * 60
        stale = not since or (self._now() - since) >= rotate_after
        bag = [t for t in (meta.get("focus_bag") or []) if t in teams and t != current]
        if current not in teams or stale or not bag:
            if not bag:
                bag = [t for t in teams if t != current] or list(teams)
            current = self.rng.choice(bag)
            meta["focus_bag"] = [t for t in bag if t != current]
            meta["focus_team"] = current
            meta["focus_since"] = self._now()
            self.world.save()
        return current

    def candidates(self, ctx):
        """`id` is stable across cycles — the clef criteria key and the decision-log key."""
        out = []
        focus = self.focus_team()
        counts = self.counts or {}
        for box in self.intel.boxes():
            ip, team, windows = box["ip"], box["team"], box["windows"]
            self._candidates_for_box(ctx, out, box, focus, counts)
        out.extend(self._candidates_global(ctx, out))
        # an effect that failed to import must not become a candidate (effects.MISSING_MODULES)
        from . import effects as effects_mod
        registered = set(effects_mod.REGISTRY)
        out = [c for c in out if c["effect"] in registered]
        for cand in out:
            cand["score"] = round(cand["score"], 2)
            # the gate reads this flag, not its own copy
            cand["high_stakes"] = bool(cand["high_stakes"]
                                       or cand["effect"] in self._high_stakes())
        out.sort(key=lambda c: (-c["score"], c["effect"],
                                str(c.get("params", {}).get("ip") or "")))
        return out

    def _high_stakes(self):
        configured = (self.cfg.get("clef") or {}).get("high_stakes_effects")
        return tuple(configured) if configured else HIGH_STAKES

    def _candidates_for_box(self, ctx, out, box, focus, counts):
        ip, team = box["ip"], box["team"]
        windows = box["windows"]
        fh = self.world.foothold(ip)
        root = can_root(fh) or (fh and str(fh.get("user", "")).lower()
                                in ("administrator", "admin"))
        score_bonus = 5.0 if team == focus else 0.0
        live_channel = self._channel_fresh(ip)
        standing = {a.get("kind") for a in self.world.artifacts(ip)}
        dead = self._dead_artifacts(ip)

        if fh is None:
            out.append(make_candidate("reestablish", {"ip": ip},
                                      BASE_SCORE["reestablish"] + score_bonus,
                                      "no foothold on this box (evicted or cold start)",
                                      cost="med", tag=f"reestablish {ip}"))
            out.append(make_candidate("spray_access", {"ip": ip},
                                      BASE_SCORE["spray_access"] + score_bonus,
                                      "no foothold; spray the known candidate pairs",
                                      cost="med", tag=f"spray {ip}"))
        else:
            if not root:
                out.append(make_candidate("escalate", {"ip": ip},
                                          BASE_SCORE["escalate"] + score_bonus,
                                          "foothold is unprivileged — everything "
                                          "useful needs root/admin",
                                          cost="low", tag=f"escalate {ip}"))

        if ctx.c2 is not None and root:
            if not live_channel:
                age = self.world.callback_age(self._box_id_for_ip(ip))
                why = ("no C2 callback recorded for this box" if age is None
                       else f"last callback {int(age)}s ago (stale)")
                fails = self.world.failure_count("implant_channel", ip)
                if fails:
                    why += f" ({fails} failed attempt(s))"
                # decay retries so a dead C2 cannot outscore pressure forever
                out.append(make_candidate("implant_channel", {"ip": ip},
                                          BASE_SCORE["implant_channel"]
                                          - 10 * fails + score_bonus,
                                          why, cost="med", tag=f"implant {ip}"))
        if root and not dead:
            if not any(k and k.startswith("persist-") for k in standing):
                out.append(make_candidate("persist", {"ip": ip, "family": "auto"},
                                          BASE_SCORE["persist"] + score_bonus,
                                          "no persistence family standing on this box",
                                          cost="med", tag=f"persist {ip}"))
        if root and dead:
            family = self._family_of(dead[0])
            windows = bool((self.world.foothold(ip) or {}).get("windows"))
            # A dead c2-implant is repaired by the implant_channel candidate above; naming
            # its kind as a persist family made the effect refuse eight times (live: cde-2026).
            if not family or family in persist_families(windows):
                out.append(make_candidate("persist",
                                          {"ip": ip, "family": family or "auto",
                                           "force": "true"},
                                          BASE_SCORE["persist"] + 15 + score_bonus,
                                          f"blue removed {len(dead)} artifact(s) here — "
                                          f"re-plant ({family or 'auto'})",
                                          cost="med", tag=f"replant {ip}"))

        if root:
            up, down = counts.get(team, (None, None))
            ok, why = self.can_impact(team)
            modes = self.allowed_impact_modes()
            strongest = self.strongest_impact_mode()
            if ok and modes and not dead:
                for service in self._pressure_targets(ctx, ip, up):
                    if service.get("down") or not service.get("scored"):
                        continue
                    mode = strongest or "stop"
                    rot, why, skip = self.pressure_rotation(ip, service["name"],
                                                            service.get("down"))
                    if skip:
                        continue
                    # One broken mechanism must not eat the window: the effect refuses
                    # after two failures, so offering it again only spends the pick.
                    # A first failure demotes it below a fresh target (live: four
                    # takedowns on the DC while web01 was never touched).
                    fails = self.world.failure_count("takedown", ip, service["name"])
                    if fails >= 2:
                        continue
                    out.append(make_candidate(
                        "takedown",
                        {"ip": ip, "service": service["name"], "mode": mode},
                        BASE_SCORE["takedown"] + rot + score_bonus - 20 * fails,
                        f"{service['name']} is a scored service on {ip}; {why}; "
                        f"mechanism {mode}"
                        + (f"; {fails} failed attempt(s) — rotate" if fails else ""),
                        cost="low", tag=f"{mode} {service['name']}@{ip}", high_stakes=True))
            elif modes and dead:
                pass          # re-plant first: a dead box cannot be pressured
            if self.unrecoverable_unlocked():
                # One successful block per port is the whole effect: re-offering it spent
                # 14 cycles re-blocking ftp:21 on cde-2026 while other services stayed up.
                blocked = {i.get("port") for i in self.world.impacts()
                           if i.get("mode") == "block_port" and i.get("ip") == ip
                           and i.get("port")}
                # A transport failure is box-wide, not per port: ad01's DC refuses SSH, and
                # red spent six cycles re-failing it on three ports (live: cde-2026).
                pins = self.intel.port_checks_for(ip)
                if sum(self.world.failure_count("block_port", ip, int(p["port"]))
                       for p in pins if p.get("port")) >= 2:
                    pins = []
                for pin in pins:
                    if not pin.get("port"):
                        continue
                    if int(pin["port"]) in blocked:
                        continue
                    # same failure-aware rule as takedown: a port whose block already
                    # failed twice is refused by the effect, so stop offering it
                    fails = self.world.failure_count("block_port", ip, int(pin["port"]))
                    if fails >= 2:
                        continue
                    out.append(make_candidate(
                        "block_port",
                        {"ip": ip, "port": int(pin["port"]),
                         "display": pin.get("display") or str(pin["port"])},
                        BASE_SCORE["block_port"] + score_bonus - 10 * fails,
                        f"{pin.get('display')} is a scored port check on {ip} — block it "
                        f"(CCDC-authentic failure mode)"
                        + (f"; {fails} failed attempt(s) — rotate" if fails else ""),
                        cost="low", high_stakes=True))
                out.append(make_candidate(
                    "rotate_credlist", {"ip": ip},
                    BASE_SCORE["rotate_credlist"] + score_bonus,
                    "rotate a credlist account so blue's own credential list fails",
                    cost="low", high_stakes=True))
            # One successful lockout per box is the whole effect: re-offering it spent a
            # cycle on an SSH call red's own lock had already closed (live: reset by peer).
            locked = any(i.get("ip") == ip and i.get("service") == "logins"
                         for i in self.world.impacts())
            if self.window_open("lockout") and not locked:
                out.append(make_candidate(
                    "lockout", {"ip": ip},
                    BASE_SCORE["lockout"] + 30 + score_bonus,
                    "lockout window is open — deny blue their own logins",
                    cost="low", high_stakes=True))

        target = self.world.target_record(ip) or {}
        if not target.get("recon_done") and self.recon_allowed():
            out.append(make_candidate("recon", {"ip": ip},
                                      BASE_SCORE["recon"] + score_bonus,
                                      "never scanned this box",
                                      cost="low", tag=f"recon {ip}"))

    def _pressure_targets(self, ctx, ip, up):
        services = self.intel.services_for(ip)
        team_status = {}
        if self.sensors is not None:
            for team_name, status in (self.sensors.team_service_map() or {}).items():
                if team_name.lower() in (self.intel.team_of(ip) or "").lower():
                    team_status = status
        out = []
        for name in services:
            slug = str(name)
            # ssh/sshd is red's own access path: the effect refuses it, so offering
            # it burns a cycle on a guaranteed failure.
            if slug.lower().removesuffix(".service").strip() in ("ssh", "sshd"):
                continue
            key = _service_key(slug)
            state = None
            for scored_name, is_up in team_status.items():
                if _scored_key(scored_name) == key:
                    state = is_up
                    break
            out.append({"name": slug,
                        "down": state is False,
                        "up": state is True,
                        # with a scoreboard, only names it scores are takedown targets
                        "scored": state is not None or not team_status})
        return out

    def _candidates_global(self, ctx, existing):
        out = []
        if not existing and ctx.cfg["llm"]["enabled"]:
            out.append(make_candidate(
                "improvise", {"ip": self._fallback_ip(), "goal": "restore momentum"},
                BASE_SCORE["improvise"],
                "no bounded candidate exists — author one command for the goal",
                cost="med", tag="improvise"))
        return out

    def _fallback_ip(self):
        box = next(iter(self.intel.boxes()), None)
        return box["ip"] if box else None

    def _box_id_for_ip(self, ip):
        """World keys its c2 map by `<name>-<identifier>`, not IP; an IP lookup reads as "no callback"."""
        box = self.intel.box_by_ip(ip)
        if not box:
            return ip
        return f"{box['box']}-{box['identifier']}"

    def _channel_fresh(self, ip):
        age = self.world.callback_age(self._box_id_for_ip(ip))
        if age is None:
            return False
        return age <= float((self.cfg.get("c2") or {}).get("callback_max_age_sec", 120))

    def _dead_artifacts(self, ip):
        dead = self.world.dead_artifacts()
        return [a for a in self.world.artifacts(ip)
                if self._artifact_key_of(a) in dead]

    @staticmethod
    def _artifact_key_of(artifact):
        from .state.world import artifact_key
        return artifact_key(artifact)

    @staticmethod
    def _family_of(artifact):
        return artifact.get("family") or (artifact.get("kind") or "").removeprefix("persist-")

    def validate(self, candidate, ctx):
        """Effects re-check their own preconditions: the seed path calls effects without this."""
        if not isinstance(candidate, dict) or "effect" not in candidate:
            raise PolicyError("candidate must be a dict with an 'effect'")
        effect = candidate["effect"]
        params = candidate.get("params") or {}
        ip = params.get("ip")
        if ip is not None and self.intel.team_of(ip) is None:
            raise PolicyError(f"{ip} is not a known team box")
        if effect in ("takedown", "block_port", "rotate_credlist", "lockout"):
            if not self.unrecoverable_unlocked() and effect != "takedown":
                raise PolicyError(f"{effect} is an unrecoverable takedown — locked until "
                                  f"the second half or the tier that unlocks it")
            if effect in ("takedown", "block_port"):
                ok, why = self.can_impact(self.intel.team_of(ip))
                if not ok:
                    raise PolicyError(why)
        if effect == "takedown":
            mode = params.get("mode") or "stop_mask"
            if mode not in self.allowed_impact_modes():
                tier = self.ladder() or {}
                raise PolicyError(
                    f"takedown mechanism {mode!r} is not unlocked at tier "
                    f"{tier.get('name', '?')!r} — allowed: "
                    f"{self.allowed_impact_modes() or 'nothing (observe)'}")
        if effect == "lockout" and not self.window_open("lockout"):
            raise PolicyError("lockout window is not open")
        return candidate

    def frame(self, ctx, candidates, recent=None, budget_tokens=8192):
        """What the model is allowed to see — the digest input."""
        boxes = []
        untouched = 0
        for box in self.intel.boxes():
            ip = box["ip"]
            fh = self.world.foothold(ip)
            services = []
            for name in self.intel.services_for(ip):
                state = "down" if self._is_down(ip, name) else "up"
                services.append([str(name), state])
            down = [s for s in services if s[1] == "down"]
            dead = len(self._dead_artifacts(ip))
            interesting = bool(fh or down or dead or box["team"] == self.focus_team())
            if not interesting:
                # never-touched fully-up boxes count instead of costing rows
                untouched += 1
                continue
            entry = {
                "id": self._box_id(box),
                "team": box["team"],
                "os": "w" if box["windows"] else "l",
                "acc": self._access_label(fh),
                "svc": (down or services)[:4],
            }
            if fh and self.world.artifacts(ip):
                entry["ch"] = "c2" if self._channel_fresh(ip) else "none"
            if dead:
                entry["dead"] = dead
            restores = self._team_restores(box["team"], 30)
            if restores or fh:
                entry["resp"] = {"restores_30m": restores,
                                 "trend": self._restore_trend(box["team"])}
            boxes.append(entry)
        frame = {
            "t": f"{int(self.elapsed_min())}/{int(self.red['duration_min'])}",
            "phase": self.event_phase(),
            "cap": self.cap_label(),
            "down": sum(d for _, d in self.counts.values() if d is not None),
            "boxes": boxes,
            "more": untouched,
            "recent": recent or [],
            "roe": {"win": (self.ladder() or {}).get("name"),
                    "modes": self.allowed_modes(),
                    "unrec": self.unrecoverable_unlocked(),
                    "teardown": self.teardown_active()},
        }
        digest, dropped = digest_mod.build(frame, budget_tokens=budget_tokens)
        if dropped:
            digest["_dropped"] = dropped
        return digest

    def _is_down(self, ip, service_name):
        status = {}
        if self.sensors is not None:
            for team_name, services in (self.sensors.team_service_map() or {}).items():
                if team_name.lower() == (self.intel.team_of(ip) or "").lower():
                    status = services
        key = _service_key(service_name)
        for scored_name, is_up in status.items():
            if _scored_key(scored_name) == key:
                return not is_up
        return False

    @staticmethod
    def _box_id(box):
        return f"{box['box']}-{box['identifier']}"

    @staticmethod
    def _access_label(foothold):
        if not foothold:
            return None
        if can_root(foothold):
            return "root" if not foothold.get("windows") else "admin"
        return "user"

    def _team_restores(self, team, minutes):
        return len([e for e in self.world.restores_within(minutes)
                    if e.get("team") == team])

    def _restore_trend(self, team):
        recent = [e for e in self.world.restores_within(15) if e.get("team") == team]
        older = [e for e in self.world.restores_within(60) if e.get("team") == team]
        if not recent:
            return "quiet"
        if len(recent) * 4 > max(len(older), 1) * 3:
            return "faster"
        return "steady"

    def question_bundle(self, digest, candidates, ctx, *, max_options=8,
                        cycle=0, include_escalation=True, include_blue_check=False,
                        compact=False, include_pressure=True):
        """Nothing invents an option candidates() omitted."""
        top = list(candidates)[:max_options]
        limit = 32 if compact else 160
        criteria = {}
        for cand in top:
            # compact: the id carries effect+target already, so text is the reason
            criteria[cand["id"]] = (cand["reason"][:limit] if compact
                                    else self._option_text(cand))
        hold = "hold"
        criteria[hold] = "hold: nothing this cycle" if compact else \
            "do nothing this cycle (consolidate, keep depth)"

        questions = {}
        meta = {"options": [c["id"] for c in top] + [hold], "compact": compact}
        if criteria:
            questions["next_effect"] = {
                "type": "choice",
                "instructions": ("Best next action: max scored-service downtime, keep "
                                 "access, obey the unlocked mechanisms."
                                 if compact else
                                 "Pick the single best next action for an assume-breach "
                                 "red team: maximize scored-service downtime, keep access, "
                                 "respect the mechanisms the ROE has unlocked."),
                "criteria": criteria,
            }
            meta["next_effect"] = {c["id"]: c["reason"] for c in top}
        if include_pressure:
            questions["pressure_count"] = {
                "type": "score",
                "instructions": (f"Extra services to take down this cycle (cap "
                                 f"{self.cap_label()}/team, tier "
                                 f"{(self.ladder() or {}).get('name', 'n/a')})?"
                                 if compact else
                                 f"Additional scored services to take down this cycle "
                                 f"(cap {self.cap_label()} per team, "
                                 f"tier {(self.ladder() or {}).get('name', 'n/a')})?"),
                "criteria": ["zero", "one", "two", "three"],
            }
        if include_escalation:
            questions["escalate_now"] = {
                "type": "noul",
                "instructions": ("Escalate to a harder/unrecoverable mechanism now?"
                                 if compact else
                                 "Escalate to a stronger/unrecoverable mechanism this "
                                 "cycle? (only if blue is keeping up)"),
            }
        if include_blue_check:
            questions["blue_hunting"] = {
                "type": "noul",
                "instructions": ("Is blue hunting/evicting us right now?"
                                 if compact else
                                 "Is blue actively hunting and evicting us right now "
                                 "(restores, logins, firewall changes)?"),
            }
        return questions, meta

    @staticmethod
    def _option_text(cand):
        return f"{cand['effect']} {cand['tag']} — {cand['reason']}"[:160]

    def request(self, digest, questions):
        return {"state": digest, "questions": questions}
