import re

from scrim_report import host_labels
from scrim_report import timefmt


HEALTH_CHECK = "health_check"

# tlaloc v2 (the in-tree red engine) writes `effect` + `params` where v1 wrote
# `tactic` + `data`: a v2 action is {"kind":"action","effect":"takedown",
# "params":{"ip":…,"service":…,"mode":…}}. Reading only the v1 shape reports a
# fully working red team as "takedowns 0, distinct tactics 1, targets none" —
# live run-8240b93c: 16 real takedowns, one gate table reading zero.
V2_IMPACT_EFFECTS = ("takedown", "block_port")
V2_INITIAL_ACCESS_EFFECTS = ("spray_access", "reestablish")


def action_effect(ev):
    """The tactic name for either schema (v1 `tactic`, v2 `effect`)."""
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


def red_metrics(events, t0, world, labels=None, win_octets=("2", "3")):
    m = {"takedowns": 0, "timeline": [], "distinct_tactics": set(), "initial_access": set(),
         "targets": set(), "stalls": [], "restore_reactions": 0, "blue_restore_events": 0,
         "blue_restore_list": [], "windows_footholds": 0, "actions_ok": 0,
         "actions_failed": 0, "evictions": 0}
    takes, ok_stamps = [], []
    prev_evicted = 0
    for ev in events:
        if ev.get("kind") != "action":
            if ev.get("kind") in ("blue_restore", "blue_restores"):
                # v1 writes one row per restore (target/detail); v2 batches them
                # under "restores": [{"team","service"}] with service "<box>-<pin>".
                # Recognising only the v1 spelling reads every v2 restore as absent
                # — the interaction gate then scores 0 restorations for a blue team
                # that logged five.
                entries = ev.get("restores")
                if isinstance(entries, list) and entries:
                    for item in entries:
                        m["blue_restore_events"] += 1
                        m["blue_restore_list"].append(
                            (timefmt.t_plus(timefmt.parse_ts(ev.get("ts", "1970")), t0),
                             (item or {}).get("team") or "?",
                             (item or {}).get("target") or "",
                             (item or {}).get("service") or (item or {}).get("detail") or ""))
                else:
                    m["blue_restore_events"] += 1
                    m["blue_restore_list"].append(
                        (timefmt.t_plus(timefmt.parse_ts(ev.get("ts", "1970")), t0), ev.get("team") or "?",
                         ev.get("target") or "", ev.get("detail", "")))
            continue
        tactic = action_effect(ev)
        ok = bool(ev.get("ok"))
        m["actions_ok" if ok else "actions_failed"] += 1
        # health_check's detail carries the agent's own eviction tally ("N footholds
        # alive, M evicted"); each rise in M is blue removing access red had.
        if tactic == HEALTH_CHECK and ok:
            em = re.search(r"(\d+) evicted", ev.get("detail", ""))
            if em:
                cur = int(em.group(1))
                if cur > prev_evicted:
                    m["evictions"] += cur - prev_evicted
                prev_evicted = cur
        ip = action_target(ev)
        tp = timefmt.t_plus(timefmt.parse_ts(ev.get("ts", "1970")), t0)
        if ok and tactic != HEALTH_CHECK:
            m["distinct_tactics"].add(tactic)
            if ip:
                m["targets"].add(ip)
            if tactic in ("cred_spray", "foothold_ssh", *V2_INITIAL_ACCESS_EFFECTS):
                m["initial_access"].add((tactic, ip))
            ok_stamps.append(tp)
        if ok and (tactic in ("impact_service", "impact_firewall")
                   or ev.get("effect") in V2_IMPACT_EFFECTS):
            dip, svc, mode = takedown_fields(ev)
            dip = dip or ip
            m["takedowns"] += 1
            m["timeline"].append((tp, host_labels.host_label(dip, labels), svc, mode))
            takes.append((tp, dip))
    per_ip = {}
    restored = []  # (tp, team, box) — box parsed from the detail; target field is null
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
        # A re-kill that closely follows blue restoring THAT box is a reaction too,
        # even when red never left (continuous pressure): red saw the restore and
        # answered it (red re-killed the same service within minutes of
        # every blue restore on the run that tuned this, but the >=15-min-gap rule counted 0 restore-reactions
        # and the gate failed red for exactly the behavior the gate is about. The
        # blue_restore event carries target=null and a detail like "web01-roundcube
        # is UP again on team2", so the match is box-level: the killed IP's team
        # octet must agree and the box label must be the restored one.
        follow_reaction = False
        box = host_labels.host_label(dip, labels)
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
        # No event_start (a run dir with red events but no world.json): every stamp is
        # None and comparing one to the stall threshold used to abort the WHOLE report —
        # including the timeout count the rehearsal gate reads. With no clock there is
        # no timeline to measure gaps in, so report none.
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
    m["targets"] = sorted(host_labels.host_label(ip, labels) for ip in m["targets"])
    return m
