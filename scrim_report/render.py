import time
from pathlib import Path

from scrim_report import blue_side
from scrim_report import gate_table
from scrim_report import host_labels
from scrim_report import loaders
from scrim_report import red_side
from scrim_report import timefmt


def build_report(run_dir):
    events, t0 = loaders.load_red_events(run_dir)
    world = loaders.load_world(run_dir)
    snaps = loaders.load_scoreboard(run_dir)
    labels = host_labels.box_labels(run_dir)
    rm = red_side.red_metrics(events, t0, world, labels, host_labels.windows_octets(run_dir))
    down = blue_side.down_windows(snaps)
    bm = blue_side.blue_metrics(run_dir)

    no_scoreboard = not snaps
    if down:
        restorations = sum(d["restorations"] for d in down.values())
        ttrs = [t for d in down.values() for t in d["ttrs_min"]]
        fast = sum(1 for t in ttrs if t <= gate_table.RESTORE_TTR_GATE_MIN)
        # per-team down-minutes are rendered straight from `down` below; a local copy was
        # built here and never used (audit find D12).
        max_sim = max((d["max_simultaneous_down"] for d in down.values()), default=0)
    else:
        restorations, ttrs, fast = rm["restore_reactions"], [], 0
        max_sim = None
    empty_room = (bm["cycles_rc0"] == 0 and restorations == 0
                  and rm["blue_restore_events"] == 0)
    score = (restorations + rm["restore_reactions"] + rm["evictions"]
             + bm["injects"] + bm["eradication"])
    gates = gate_table.evaluate(
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
             + (" (inferred from re-kills; no scoreboard series)" if no_scoreboard else "")
             + f", red restore-reactions {rm['restore_reactions']}, "
             f"evictions {rm['evictions']}, "
             f"injects {bm['injects']}, eradication {bm['eradication']}.\n")

    L.append("## Red\n")
    if events and not labels:
        L.append("_Note: no boxes.json could be resolved for this run dir, so host labels "
                 "below are raw addresses rather than box names (the old fixed 17b table "
                 "would have invented the wrong names)._\n")
    L.append(f"- takedowns: **{rm['takedowns']}**")
    for tp, host, svc, mode in rm["timeline"]:
        L.append(f"  - {timefmt.fmt_t(tp)} {host} {svc} ({mode})")
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
        L.append(f"  - {timefmt.fmt_t(a)} -> {timefmt.fmt_t(b)} ({gap:.0f} min)")
    L.append(f"- actions: {rm['actions_ok']} ok / {rm['actions_failed']} failed\n")

    L.append("## Blue interaction\n")
    L.append(f"- explicit blue_restore events seen by red: **{rm['blue_restore_events']}**")
    for tp, team, ip, detail in rm["blue_restore_list"]:
        L.append(f"  - {timefmt.fmt_t(tp)} {team} {host_labels.host_label(ip, labels)}: {detail}")
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
            L.append(f"- time-to-restore <= {gate_table.RESTORE_TTR_GATE_MIN} min: **{fast}** of {len(ttrs)}")
    else:
        L.append("- restorations/down-minutes: **n/a** (run predates scoreboard-state.jsonl; "
                 "restoration count above is inferred from red's re-kills)")
    L.append(f"- injects submitted: **{bm['injects']}**")
    L.append(f"- notebook liveliness: **{bm['notebook_entries']}** entries\n")

    final = loaders.load_final_scoreboard(run_dir)
    if final:
        L.append("## Final scores (evidence dump at capture, before teardown)\n")
        for team, rows in sorted((final.get("services") or {}).items()):
            if rows is None:
                L.append(f"- {team}: capture failed")
                continue
            downs = [r["service"] for r in rows if not r.get("up")]
            L.append(f"- {team}: {len(rows) - len(downs)} up / {len(downs)} down"
                     + (f" — down: {', '.join(downs)}" if downs else ""))
        injects = final.get("injects") or []
        subs = sum(1 for inj in injects for s in (inj.get("Submissions") or []))
        L.append(f"- injects at capture: {len(injects)} published, {subs} submissions\n")

    alerts = loaders.load_alerts(run_dir)
    if alerts:
        L.append("## Run alerts (evidence/alerts.jsonl)\n")
        for a in alerts:
            L.append(f"- {a.get('ts', '?')} **{a.get('kind', '?')}** — {a.get('detail', '')}")
        L.append("")
    elif (Path(run_dir) / "evidence").is_dir():
        L.append("## Run alerts\n")
        L.append("- none\n")

    L.append("## Gates (docs/rehearsal-gates.md)\n")
    L.append("| side | gate | value | threshold | verdict |")
    L.append("|---|---|---|---|---|")
    for section, key, val, threshold, verdict_cell in gates:
        L.append(f"| {section} | {key} | {val} | {gate_table.op_str(threshold)} | {verdict_cell} |")
    L.append("")
    if no_scoreboard:
        L.append("Data limitations: no scoreboard-state.jsonl in this run dir — "
                 "down-minutes, time-to-restore, max-simultaneous-down and true "
                 "restoration counts are unavailable; red-side numbers are the "
                 "reliable record.\n")
    return "\n".join(L) + "\n", {"score": score, "gates_failed": len(gates_failed)}
