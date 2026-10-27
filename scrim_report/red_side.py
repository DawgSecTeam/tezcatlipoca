import re

from scrim_report import host_labels
from scrim_report import timefmt


HEALTH_CHECK = "health_check"

# tlaloc v2 writes `effect` + `params` where v1 wrote `tactic` + `data`; reading only
# the v1 shape scores a fully working red team at zero takedowns.
V2_TAKEDOWN_EFFECTS = ("takedown",)
V2_INITIAL_ACCESS_EFFECTS = ("spray_access", "reestablish")


# SSH is red's own access path, never a takedown target (effects.allow_ssh_takedown stays off).
ACCESS_SERVICE_PINS = ("ssh", "sshd")


def is_access_service(name):
    return str(name or "").rsplit("-", 1)[-1] in ACCESS_SERVICE_PINS


def action_effect(ev):
    return ev.get("tactic") or ev.get("effect") or "?"


def action_target(ev):
    return ev.get("target") or (ev.get("params") or {}).get("ip") or None


STALL_SEC = 600


REAKILL_GAP_SEC = 900


def takedown_fields(ev):
    data = ev.get("data") or ev.get("params") or {}
    detail = ev.get("detail", "")
    ip = data.get("ip") or ev.get("target")
    svc = data.get("unit") or data.get("service")
    mode = data.get("mode") or ""
    if not svc:
        m = re.search(r"(\S+?) is DOWN on ", detail)
        svc = m.group(1) if m else "?"
    if not ip:
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", detail)
        ip = m.group(1) if m else None
    return ip, svc, mode or ("firewall" if action_effect(ev) == "impact_firewall" else "stop")


def red_metrics(events, t0, world, labels=None, win_octets=("2", "3"), teams=None):
    m = {"takedowns": 0, "timeline": [], "distinct_tactics": set(), "initial_access": set(),
         "targets": set(), "stalls": [], "restore_reactions": 0, "blue_restore_events": 0,
         "blue_restore_list": [], "windows_footholds": 0, "actions_ok": 0,
         "actions_failed": 0, "evictions": 0}
    takes, ok_stamps = [], []
    prev_evicted = 0
    for ev in events:
        if ev.get("kind") != "action":
            if ev.get("kind") in ("blue_restore", "blue_restores"):
                # v2 batches restores under a "restores" list; v1 writes one row per restore.
                entries = ev.get("restores")
                if isinstance(entries, list) and entries:
                    for item in entries:
                        m["blue_restore_events"] += 1
                        m["blue_restore_list"].append(
                            (timefmt.t_plus(timefmt.parse_ts(ev.get("ts") or ""), t0),
                             (item or {}).get("team") or "?",
                             (item or {}).get("target") or "",
                             (item or {}).get("service") or (item or {}).get("detail") or ""))
                else:
                    m["blue_restore_events"] += 1
                    m["blue_restore_list"].append(
                        (timefmt.t_plus(timefmt.parse_ts(ev.get("ts") or ""), t0), ev.get("team") or "?",
                         ev.get("target") or "", ev.get("detail", "")))
            continue
        tactic = action_effect(ev)
        ok = bool(ev.get("ok"))
        m["actions_ok" if ok else "actions_failed"] += 1
        # health_check detail "N footholds alive, M evicted": a rise in M is blue evicting red.
        if tactic == HEALTH_CHECK and ok:
            em = re.search(r"(\d+) evicted", ev.get("detail", ""))
            if em:
                cur = int(em.group(1))
                if cur > prev_evicted:
                    m["evictions"] += cur - prev_evicted
                prev_evicted = cur
        ip = action_target(ev)
        tp = timefmt.t_plus(timefmt.parse_ts(ev.get("ts") or ""), t0)
        if ok and tactic != HEALTH_CHECK:
            m["distinct_tactics"].add(tactic)
            if ip:
                m["targets"].add(ip)
            if tactic in ("cred_spray", "foothold_ssh", *V2_INITIAL_ACCESS_EFFECTS):
                m["initial_access"].add((tactic, ip))
            ok_stamps.append(tp)
        if ok and (tactic in ("impact_service", "impact_firewall")
                   or ev.get("effect") in V2_TAKEDOWN_EFFECTS):
            dip, svc, mode = takedown_fields(ev)
            dip = dip or ip
            m["takedowns"] += 1
            m["timeline"].append((tp, host_labels.host_label(dip, labels, teams), svc, mode))
            takes.append((tp, dip))
    per_ip = {}
    restored = []
    for rtp, _team, rtarget, rdetail in m["blue_restore_list"]:
        box = None
        for name in (labels or {}).values() if labels else []:
            if rdetail and rdetail.startswith(name):
                box = name
                break
        if box is None and rdetail:
            box = rdetail.split("-")[0] if "-" in rdetail else None
        restored.append((rtp, _team, box))
    for tp, dip in takes:
        if dip is None:
            continue
        earlier = per_ip.get(dip)
        gap_reaction = (earlier is not None
                        and (tp - earlier) * 60 >= REAKILL_GAP_SEC)
        # A re-kill soon after blue restored that box is a reaction even with no gap;
        # restore rows carry target=null, so the match is on the host label.
        follow_reaction = False
        box = host_labels.host_label(dip, labels, teams)
        for rtp, _rteam, rbox in restored:
            if (rtp is not None and tp is not None
                    and 0 <= tp - rtp <= REAKILL_GAP_SEC / 60
                    and rbox and rbox in box):
                follow_reaction = True
                break
        if gap_reaction or follow_reaction:
            m["restore_reactions"] += 1
        if earlier is None or tp > earlier:
            per_ip[dip] = tp
    if t0 is None:
        # No t0: stamps are None and comparing one to the threshold aborted the whole report.
        ok_stamps = [s for s in ok_stamps if s is not None]
    if ok_stamps and ok_stamps[0] >= STALL_SEC / 60.0:
        m["stalls"].append((0.0, ok_stamps[0], ok_stamps[0]))
    for a, b in zip(ok_stamps, ok_stamps[1:]):
        if b - a >= STALL_SEC / 60.0:
            m["stalls"].append((a, b, b - a))
    # world.json 'footholds' has been dict-keyed-by-ip or a list across versions
    fh = world.get("footholds") or []
    for f in (list(fh.values()) if isinstance(fh, dict) else fh):
        if isinstance(f, dict) and f.get("windows"):
            m["windows_footholds"] += 1
    if not m["windows_footholds"]:
        m["windows_footholds"] = len({ip for _, ip in m["initial_access"]
                                      if ip and ip.split(".")[-1] in win_octets})
    m["distinct_tactics"] = sorted(m["distinct_tactics"])
    m["initial_access"] = sorted({t for t, _ in m["initial_access"]})
    m["targets"] = sorted(host_labels.host_label(ip, labels, teams) for ip in m["targets"])
    return m
