#!/usr/bin/env python3
"""Interaction report for an agent-scrim run dir — writes INTERACTION.md next to FINDINGS.md."""

import argparse
import calendar
import json
import re
import sys
import time
from pathlib import Path

HOST_BY_OCTET = {"2": "dc01", "3": "win01", "4": "web01", "5": "app01", "6": "db01"}
HEALTH_CHECK = "health_check"
STALL_SEC = 600
REAKILL_GAP_SEC = 900
RESTORE_TTR_GATE_MIN = 15

GATES = {
    "red": [("takedowns", 6, ">="), ("restore_reactions", 3, ">="),
            ("distinct_tactics", 4, ">="), ("windows_footholds", 1, ">="),
            ("max_simultaneous_down", 4, ">="), ("stalls", 0, "=="),
            ("evictions", 1, ">=")],
    "blue": [("cycles_rc0", 8, ">="), ("restorations", 2, ">="),
             ("injects", 2, ">="), ("notebook_entries", 10, ">="),
             ("timeouts", 0, "==")],
}

EXPECTED_17C = {
    "takedowns": 4, "distinct_tactics": 3, "restore_reactions": 1,
    "restorations": 1, "injects": 0, "interaction_score": 2, "stalls": 2,
    "evictions": 0, "timeouts": 0,
}


def parse_ts(ts):
    """ISO ts on red01's clock (UTC, optional fraction) -> epoch."""
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))


def t_plus(ts_epoch, t0):
    return None if t0 is None else (ts_epoch - t0) / 60.0


def fmt_t(tp):
    return f"T+{tp:.0f}" if tp is not None else "?"


def host_label(ip):
    if not ip:
        return "?"
    octets = ip.split(".")
    host = HOST_BY_OCTET.get(octets[-1], f".{octets[-1]}" if len(octets) == 4 else ip)
    team = {"101": "1", "102": "2"}.get(octets[2], "") if len(octets) == 4 else ""
    return f"{team}:{host}" if team else host



def load_red_events(run_dir):
    """Merge final events.jsonl with in-run snapshots, window-filtered and deduplicated."""
    red = Path(run_dir) / "evidence" / "red"
    files = sorted(red.glob("events*.jsonl")) if red.is_dir() else []
    events, seen = [], set()
    for path in files:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            key = (ev.get("ts"), ev.get("kind"), ev.get("tactic"),
                   ev.get("target"), ev.get("detail"))
            if key in seen:
                continue
            seen.add(key)
            events.append(ev)
    t0 = load_event_start(run_dir)
    if t0 is not None:
        events = [e for e in events
                  if t0 - 120 <= parse_ts(e.get("ts", "")) <= t0 + 8 * 3600]
    events.sort(key=lambda e: parse_ts(e.get("ts", "1970")))
    return events, t0


def load_event_start(run_dir):
    for cand in (Path(run_dir) / "evidence" / "red" / "world.json",
                 Path(run_dir) / "bad-auto-state" / "world.json"):
        try:
            w = json.loads(cand.read_text())
            t0 = (w.get("meta") or {}).get("event_start")
            if t0:
                return float(t0)
        except (OSError, ValueError):
            continue
    return None


def load_world(run_dir):
    for cand in (Path(run_dir) / "evidence" / "red" / "world.json",
                 Path(run_dir) / "bad-auto-state" / "world.json"):
        try:
            return json.loads(cand.read_text())
        except (OSError, ValueError):
            continue
    return {}


def load_scoreboard(run_dir):
    """[(t_plus_sec, {team: {service: up}})] or [] when the run predates C1."""
    path = Path(run_dir) / "scoreboard-state.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        out.append((rec.get("t_plus_sec", 0),
                    {t: {s["service"]: s["up"] for s in (svcs or [])}
                     for t, svcs in (rec.get("teams") or {}).items()}))
    return sorted(out)



def takedown_fields(ev):
    data = ev.get("data") or {}
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
    return ip, svc, mode or ("firewall" if ev.get("tactic") == "impact_firewall" else "stop")


def foothold_list(world):
    """world.json 'footholds' has been dict-keyed-by-ip or a list across versions."""
    fh = world.get("footholds") or []
    return list(fh.values()) if isinstance(fh, dict) else fh


def red_metrics(events, t0, world):
    m = {"takedowns": 0, "timeline": [], "distinct_tactics": set(), "initial_access": set(),
         "targets": set(), "stalls": [], "restore_reactions": 0, "blue_restore_events": 0,
         "blue_restore_list": [], "windows_footholds": 0, "actions_ok": 0,
         "actions_failed": 0, "evictions": 0}
    takes, ok_stamps = [], []
    prev_evicted = 0
    for ev in events:
        if ev.get("kind") != "action":
            if ev.get("kind") == "blue_restore":
                m["blue_restore_events"] += 1
                m["blue_restore_list"].append(
                    (t_plus(parse_ts(ev.get("ts", "1970")), t0), ev.get("team") or "?",
                     ev.get("target") or "", ev.get("detail", "")))
            continue
        tactic = ev.get("tactic", "?")
        ok = bool(ev.get("ok"))
        m["actions_ok" if ok else "actions_failed"] += 1
        # health_check's detail carries bad-auto's own eviction tally ("N footholds
        # alive, M evicted"); each rise in M is blue removing access red had.
        if tactic == HEALTH_CHECK and ok:
            em = re.search(r"(\d+) evicted", ev.get("detail", ""))
            if em:
                cur = int(em.group(1))
                if cur > prev_evicted:
                    m["evictions"] += cur - prev_evicted
                prev_evicted = cur
        ip = ev.get("target")
        tp = t_plus(parse_ts(ev.get("ts", "1970")), t0)
        if ok and tactic != HEALTH_CHECK:
            m["distinct_tactics"].add(tactic)
            if ip:
                m["targets"].add(ip)
            if tactic in ("cred_spray", "foothold_ssh"):
                m["initial_access"].add((tactic, ip))
            ok_stamps.append(tp)
        if ok and tactic in ("impact_service", "impact_firewall"):
            dip, svc, mode = takedown_fields(ev)
            dip = dip or ip
            m["takedowns"] += 1
            m["timeline"].append((tp, host_label(dip), svc, mode))
            takes.append((tp, dip))
    per_ip = {}
    for tp, dip in takes:
        if dip is None:
            continue
        earlier = per_ip.get(dip)
        if earlier is not None and (tp - earlier) * 60 >= REAKILL_GAP_SEC:
            m["restore_reactions"] += 1
        if earlier is None or tp > earlier:
            per_ip[dip] = tp
    if ok_stamps and ok_stamps[0] >= STALL_SEC / 60.0:
        m["stalls"].append((0.0, ok_stamps[0], ok_stamps[0]))
    for a, b in zip(ok_stamps, ok_stamps[1:]):
        if b - a >= STALL_SEC / 60.0:
            m["stalls"].append((a, b, b - a))
    for f in foothold_list(world):
        if isinstance(f, dict) and f.get("windows"):
            m["windows_footholds"] += 1
    if not m["windows_footholds"]:
        m["windows_footholds"] = len({ip for _, ip in m["initial_access"]
                                      if ip and ip.split(".")[-1] in ("2", "3")})
    m["distinct_tactics"] = sorted(m["distinct_tactics"])
    m["initial_access"] = sorted({t for t, _ in m["initial_access"]})
    m["targets"] = sorted(host_label(ip) for ip in m["targets"])
    return m


def down_windows(snaps):
    """Per team: restorations, ttrs (min), down-minutes, max simultaneous down."""
    if not snaps:
        return None
    teams = sorted({t for _, teams in snaps for t in teams})
    out = {}
    for team in teams:
        series = [(tp, states.get(team, {})) for tp, states in snaps]
        if not series:
            continue
        services = sorted({s for _, states in series for s in states})
        windows, ttrs, down_sec = [], [], 0.0
        for svc in services:
            down_at = None
            for tp, states in series:
                up = states.get(svc, True)
                if not up and down_at is None:
                    down_at = tp
                elif up and down_at is not None:
                    windows.append((down_at, tp, svc))
                    ttrs.append(tp - down_at)
                    down_at = None
            if down_at is not None:
                end = series[-1][0]
                windows.append((down_at, end, svc))
                down_sec += end - down_at
        max_sim = 0
        for tp, states in series:
            max_sim = max(max_sim, sum(1 for s in services if states.get(s) is False))
        out[team] = {"restorations": len(ttrs), "ttrs_min": [t / 60 for t in ttrs],
                     "down_min": down_sec / 60, "max_simultaneous_down": max_sim,
                     "windows": windows}
    return out



def blue_metrics(run_dir):
    m = {"cycles_rc0": 0, "cycles_total": 0, "manual_rc0": 0, "timeouts": 0, "injects": 0,
         "notebook_entries": 0, "eradication": 0}
    erad_re = re.compile(
        r"(tznet|svc-netupdate|TzNet|red_key|authorized_keys|backdoor|rogue|"
        r"uid\s*=?\s*0|unauthorized)", re.I)
    erad_verbs = re.compile(r"(removed|deleted|disabled|uninstalled|locked|changed.*back|reset)", re.I)
    for n in (1, 2):
        wd = Path(run_dir) / f"blue-team{n}"
        if not wd.is_dir():
            continue
        feed = wd / "feed.log"
        if feed.exists():
            for line in feed.read_text(errors="replace").splitlines():
                hdr = re.match(r"===== (MANUAL )?cycle .* rc=(\d+)", line.strip())
                if hdr:
                    m["cycles_total"] += 1
                    if hdr.group(2) == "0":
                        m["cycles_rc0"] += 1
                        if hdr.group(1):
                            m["manual_rc0"] += 1
                elif re.match(r"===== cycle .* TIMEOUT", line.strip()):
                    m["timeouts"] += 1
        log_text = ""
        for name in ("LOG.md", "NOTEBOOK.md"):
            p = wd / name
            if p.exists():
                text = p.read_text(errors="replace")
                log_text += text + "\n"
                if name == "LOG.md":
                    m["notebook_entries"] += sum(
                        1 for line in text.splitlines()
                        if line.strip() and not line.startswith("#"))
                else:
                    m["notebook_entries"] += sum(
                        1 for line in text.splitlines() if line.strip().startswith("- [x]"))
        for line in log_text.splitlines():
            if erad_re.search(line) and erad_verbs.search(line):
                m["eradication"] += 1
        injects = set(wd.glob("sub-*.md")) | set(wd.glob("sub-*.txt"))
        subs = wd / "submissions"
        if subs.is_dir():
            injects |= {p for p in subs.iterdir() if p.is_file()}
        m["injects"] += len(injects)
    return m



def evaluate(gm, bm):
    rows = []
    for section, metrics in (("red", gm), ("blue", bm)):
        for key, threshold, op in GATES[section]:
            val = metrics.get(key)
            if val is None:
                rows.append((section, key, "n/a", threshold, "n/a"))
                continue
            ok = val >= threshold if op == ">=" else val == threshold
            rows.append((section, key, val, threshold, "PASS" if ok else "FAIL"))
    return rows


def build_report(run_dir):
    events, t0 = load_red_events(run_dir)
    world = load_world(run_dir)
    snaps = load_scoreboard(run_dir)
    rm = red_metrics(events, t0, world)
    down = down_windows(snaps)
    bm = blue_metrics(run_dir)

    legacy = not snaps
    if down:
        restorations = sum(d["restorations"] for d in down.values())
        ttrs = [t for d in down.values() for t in d["ttrs_min"]]
        fast = sum(1 for t in ttrs if t <= RESTORE_TTR_GATE_MIN)
        down_min = {t: d["down_min"] for t, d in down.items()}
        max_sim = max((d["max_simultaneous_down"] for d in down.values()), default=0)
    else:
        restorations, ttrs, fast = rm["restore_reactions"], [], 0
        down_min, max_sim = None, None
    empty_room = (bm["cycles_rc0"] == 0 and restorations == 0
                  and rm["blue_restore_events"] == 0)
    score = (restorations + rm["restore_reactions"] + rm["evictions"]
             + bm["injects"] + bm["eradication"])
    gates = evaluate(
        {"takedowns": rm["takedowns"], "restore_reactions": rm["restore_reactions"],
         "distinct_tactics": len(rm["distinct_tactics"]),
         "windows_footholds": rm["windows_footholds"], "max_simultaneous_down": max_sim,
         "stalls": len(rm["stalls"]), "evictions": rm["evictions"]},
        {"cycles_rc0": bm["cycles_rc0"], "restorations": restorations,
         "injects": bm["injects"], "notebook_entries": bm["notebook_entries"],
         "timeouts": bm["timeouts"]})
    gates_failed = [r for r in gates if r[4] == "FAIL"]

    L = []
    name = Path(run_dir).resolve().name
    L.append(f"# INTERACTION — {name}\n")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M')} from "
             f"{len(events)} red events"
             + (f", {len(snaps)} scoreboard snapshots" if snaps else "") + ".\n")
    L.append("## Verdict\n")
    if empty_room:
        verdict = ("**NO INTERACTION — blue never engaged** (0 successful cycles, no "
                   "restorations, no blue_restore events). Red ran against an empty room; "
                   "per the reporting rule this is not a red success, whatever the numbers say.")
    elif score == 0:
        verdict = ("**FAILED RUN — interaction score 0: parallel monologues.** "
                   "Red and blue never touched the same game. No matter how good "
                   "red's log looks, this run taught nobody anything.")
    elif gates_failed:
        verdict = (f"**NOT READY — interaction score {score}, "
                   f"{len(gates_failed)} gate(s) failed** (see Gates below).")
    else:
        verdict = f"**GREEN — interaction score {score}, all gates passed.**"
    L.append(f"{verdict}\n")
    L.append(f"Interaction score components: restorations {restorations}"
             + (" (inferred from re-kills; no scoreboard series)" if legacy else "")
             + f", red restore-reactions {rm['restore_reactions']}, "
             f"evictions {rm['evictions']}, "
             f"injects {bm['injects']}, eradication {bm['eradication']}.\n")

    L.append("## Red\n")
    L.append(f"- takedowns: **{rm['takedowns']}**")
    for tp, host, svc, mode in rm["timeline"]:
        L.append(f"  - {fmt_t(tp)} {host} {svc} ({mode})")
    L.append(f"- distinct tactics (excl. health_check): **{len(rm['distinct_tactics'])}** "
             f"({', '.join(rm['distinct_tactics']) or 'none'})")
    L.append(f"- initial access: **{len(rm['initial_access'])}** technique(s) "
             f"({', '.join(rm['initial_access']) or 'none'})")
    L.append(f"- targets touched: {', '.join(rm['targets']) or 'none'}")
    L.append(f"- restore-reactions (re-kill after >=15 min): **{rm['restore_reactions']}**"
             + (f"; explicit blue_restore events: {rm['blue_restore_events']}"
                if rm["blue_restore_events"] else ""))
    L.append(f"- Windows footholds: **{rm['windows_footholds']}**")
    L.append(f"- stalls >=10 min without a successful new action: **{len(rm['stalls'])}**")
    for a, b, gap in rm["stalls"]:
        L.append(f"  - {fmt_t(a)} -> {fmt_t(b)} ({gap:.0f} min)")
    L.append(f"- actions: {rm['actions_ok']} ok / {rm['actions_failed']} failed\n")

    L.append("## Blue interaction\n")
    L.append(f"- explicit blue_restore events seen by red: **{rm['blue_restore_events']}**")
    for tp, team, ip, detail in rm["blue_restore_list"]:
        L.append(f"  - {fmt_t(tp)} {team} {host_label(ip)}: {detail}")
    if not rm["blue_restore_list"]:
        L.append("  (none — blue never restored while red was watching, or this run "
                 "predates blue_restore logging)")
    L.append(f"- evictions (footholds blue removed, from red's own health checks): "
             f"**{rm['evictions']}**\n")

    L.append("## Blue\n")
    L.append(f"- cycles rc=0: **{bm['cycles_rc0']}** of {bm['cycles_total']} logged"
             f" (manual rc=0: {bm['manual_rc0']}, timeouts: {bm['timeouts']})")
    if down:
        for team, d in sorted(down.items()):
            L.append(f"- {team}: restorations **{d['restorations']}**, "
                     f"down **{d['down_min']:.0f} min** total, "
                     f"max simultaneous down {d['max_simultaneous_down']}")
            for t in d["ttrs_min"]:
                L.append(f"  - time-to-restore {t:.0f} min")
        if ttrs:
            L.append(f"- time-to-restore <= {RESTORE_TTR_GATE_MIN} min: **{fast}** of {len(ttrs)}")
    else:
        L.append("- restorations/down-minutes: **n/a** (run predates scoreboard-state.jsonl; "
                 "restoration count above is inferred from red's re-kills)")
    L.append(f"- injects submitted: **{bm['injects']}**")
    L.append(f"- notebook liveliness: **{bm['notebook_entries']}** entries\n")

    L.append("## Gates (docs/rehearsal-gates.md)\n")
    L.append("| side | gate | value | threshold | verdict |")
    L.append("|---|---|---|---|---|")
    for section, key, val, threshold, verdict_cell in gates:
        L.append(f"| {section} | {key} | {val} | {op_str(threshold)} | {verdict_cell} |")
    L.append("")
    if legacy:
        L.append("Data limitations: no scoreboard-state.jsonl in this run dir — "
                 "down-minutes, time-to-restore, max-simultaneous-down and true "
                 "restoration counts are unavailable; red-side numbers are the "
                 "reliable record. Future runs get the full set.\n")
    return "\n".join(L) + "\n", {"score": score, "gates_failed": len(gates_failed)}


def op_str(threshold):
    return f"== {threshold}" if threshold == 0 else f">= {threshold}"


def compute_components(run_dir):
    """Score components alone, for the pinned self-test."""
    events, t0 = load_red_events(run_dir)
    world = load_world(run_dir)
    snaps = load_scoreboard(run_dir)
    rm = red_metrics(events, t0, world)
    down = down_windows(snaps)
    bm = blue_metrics(run_dir)
    restorations = (sum(d["restorations"] for d in down.values()) if down
                    else rm["restore_reactions"])
    return {"takedowns": rm["takedowns"], "distinct_tactics": len(rm["distinct_tactics"]),
            "restore_reactions": rm["restore_reactions"], "restorations": restorations,
            "injects": bm["injects"], "stalls": len(rm["stalls"]),
            "evictions": rm["evictions"], "timeouts": bm["timeouts"],
            "interaction_score": restorations + rm["restore_reactions"]
            + rm["evictions"] + bm["injects"] + bm["eradication"]}


def self_test(run_dir):
    comp = compute_components(run_dir)
    bad = [f"  {k}: expected {v}, got {comp.get(k)}"
           for k, v in EXPECTED_17C.items() if comp.get(k) != v]
    if bad:
        print("SELF-TEST FAILED (pin mismatch — re-check against FINDINGS.md evidence):")
        print("\n".join(bad))
        return 1
    print(f"self-test OK: {Path(run_dir).name} scores exactly the pinned 17c numbers")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing INTERACTION.md")
    ap.add_argument("--self-test", action="store_true",
                    help="assert the pinned agent-scrim-2026-09-17c numbers")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        sys.exit(f"no such run dir: {run_dir}")
    report, summary = build_report(run_dir)
    if args.stdout:
        print(report)
    else:
        out = run_dir / "INTERACTION.md"
        out.write_text(report)
        print(f"wrote {out}")
    print(f"interaction score: {summary['score']} "
          f"({summary['gates_failed']} gate(s) failed)")
    if args.self_test:
        sys.exit(self_test(run_dir))


if __name__ == "__main__":
    main()
