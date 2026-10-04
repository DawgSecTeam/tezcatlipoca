import json
from pathlib import Path

from scrim import compworld
from scrim import core
from scrim import quotient_api
from scrim.runfiles import SCOREBOARD_STATE


def blue_cycle_prompt(n, creds, args, elapsed, remain, delta_text, inject_text, notebook_text, log_tail):
    tid = creds[f"TEAM{n}_ID"]
    world = compworld.comp_world(core.REPO / "competitions" / args.competition)
    lin, win = world["linux"], world["windows"]
    unit = getattr(args, "web_unit", None) or world["web_unit"]
    title = world["name"] or "the company network"
    header = f"You are Blue Team {n} defending {title} (practice competition)."
    if world["scenario"]:
        header += f"\nScenario: {world['scenario']}"
    reach = [f"REACH YOUR BOXES (subnet 192.168.{tid}.0/24):"]
    if lin:
        rest = ", ".join(lin[1:]) or "none"
        reach.append(f"  Linux ({creds['BOX_USER']}, password in scrim.env, sudo: echo $BOX_PW | sudo -S <cmd>):"
                     f"  ./mybox {lin[0]} \"<cmd>\"   (also: {rest})")
    if win:
        rest = ", ".join(win[1:]) or "none"
        reach.append(f"  Windows (Administrator, same password):"
                     f"  ./mybox {win[0]} \"<cmd>\"   (also: {rest})")
    reach.append("  ./mybox runs one command through the gateway and prints output — prefer it over hand-building ssh.")
    scoring = ["HOW THE SCORER SEES YOUR BOXES — check with ./scorch after ANY change to sshd, a firewall, or a scored service:",
               "  ./scorch runs every scored check from the engine's vantage. The ssh pins log in as a credlist",
               f"  account (airship/blueteam) WITH A PASSWORD from the gateway 192.168.{tid}.1 — if you set",
               "  PasswordAuthentication no, the scored check itself fails and the pin shows DOWN forever even",
               "  though sshd is healthy locally. Safe hardening is a gateway-only exception:",
               f"    Match User airship Address 192.168.{tid}.1",
               "        PasswordAuthentication yes",
               "  (global PasswordAuthentication no), then sshd -t, reload ssh, and ./scorch must show the ssh pin UP."]
    if unit and lin:
        restore_example = (f'(./mybox {lin[0]} "echo $BOX_PW | sudo -S systemctl unmask {unit}; '
                           f'echo $BOX_PW | sudo -S systemctl start {unit}" for example)')
    else:
        restore_example = "(restart the failed unit over ./mybox for example)"
    return f"""{header}
T+{elapsed}min of {args.duration_min} ({remain}min left). Work in THIS directory; everything you need is here.

CHANGES SINCE LAST CYCLE (orchestrator scoreboard diff — act on these first):
{delta_text}

LIVE SCOREBOARD (your scored services — availability is points every minute):
{quotient_api.status_text(creds, f'team{n}')}

INJECTS:
{inject_text}

TEAM NOTEBOOK (NOTEBOOK.md — your working memory; current content):
{notebook_text}

Last LOG.md lines:
{log_tail}

{chr(10).join(reach)}

{chr(10).join(scoring)}

SCOREBOARD + INJECTS — Quotient allows ONE session per account, so NEVER log in
directly (that kills the shared jar's session). Use the shipped helpers — they
share the jar, re-login on rejection, and resolve the engine's INTERNAL team id
themselves ($MY_TID is your SUBNET number; the services API rejects it with
{{"error":"Forbidden"}} forever — live-found 2026-10-03, both teams flew blind
on it):
  ./score.py          # your team's services, scorer's-eye view (UP/DOWN per pin)
  ./myscore           # same, one line per service
  ./qlogin            # ONLY if a helper still answers Forbidden after one retry
  ./submit-inject <injectId> submissions/sub-<injectId>.md   # submit BEFORE close time
  curl -s -b "$JAR" http://$ENGINE_IP/api/injects | python3 -c "import json,sys;[print(i['ID'],i['Title'],'due',i['DueTime'][11:16],'subs',len(i.get('Submissions') or [])) for i in json.load(sys.stdin)]"

CYCLE TASK — you have ~25 wall-clock minutes for this whole cycle; pace for it,
update the notebook BEFORE acting (so an interrupted cycle still hands over context),
then AT MOST TWO change/fix actions, then STOP. Read-only investigation (status queries,
hunts) is fine inside your budget — two changes is the cap, not two commands:
0. FIRST: update NOTEBOOK.md (SNAPSHOT line: current state + next action; move finished
   items to DONE; add new incidents/findings) and append one timestamped line to LOG.md.
1. If any service above is DOWN (or CHANGES shows a new DOWN): restore it NOW {restore_example}. Availability beats everything.
   If the SAME service goes down again after you restored it, the attacker still has a way in —
   close the ENTRY VECTOR in the same cycle (e.g. a harvested password over SSH: set
   `PasswordAuthentication no` in sshd_config AND any sshd_config.d drop-in, reload ssh — your
   own access is key-based; rotate/lock the abused account). Restoring the symptom alone gets
   re-undone every minute.
2. Else if an inject is due within 30 minutes and unsubmitted: investigate on the boxes, write the deliverable, ./submit-inject.
3. Else: ONE hunt item from the notebook checklist (rogue UID-0 users, cron, systemd units, sudoers, firewall rules, listeners, Windows services/tasks/run-keys). Fix what is safe; never take a scored service down.
4. Wrap up by minute 20: finish the current step, re-check the scoreboard, leave the
   SNAPSHOT line + LOG.md current so the next cycle starts warm.
AFTER-ACTION REPORT: when you conclude — at the end of your last cycle, or as soon as you
   know you cannot continue — write REPORT.md in your workdir. An operator reads it after
   the event to grade the defence, so make it self-contained: what you found, what you
   restored or fixed and how long each took, which injects you submitted, what you could
   not do, and what you would change about this competition.
ROE: never attack the engine ($ENGINE_IP) or the scoring path; never lock out, rename, or
rotate {creds['BOX_USER']}/Administrator — those are the credentials your own helpers use.
Keep replies terse."""


def scoreboard_delta(run_dir, team):
    """CHANGES SINCE LAST CYCLE for one team, from scoreboard-state.jsonl."""
    path = Path(run_dir) / SCOREBOARD_STATE
    try:
        recs = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return "(no scoreboard history yet)"

    def states(rec):
        return {s["service"]: s["up"] for s in ((rec or {}).get("teams", {}).get(team) or [])}

    cur = recs[-1]
    curm = states(cur)
    if len(recs) < 2:
        downs = sorted(s for s, up in curm.items() if not up)
        return f"baseline snapshot T+{cur['t_plus_sec'] // 60}; DOWN at baseline: {', '.join(downs) or 'none'}"
    prevm = states(recs[-2])
    lines = []
    for svc, up in curm.items():
        if svc not in prevm:
            continue
        if prevm[svc] and not up:
            lines.append(f"{svc} DOWN (new since last cycle) — RESTORE IT FIRST")
        elif not prevm[svc] and up:
            lines.append(f"{svc} back UP — expect red to re-attack it")
        elif not up:
            since = cur["t_plus_sec"]
            for r in recs:
                if states(r).get(svc, True):
                    continue
                since = r["t_plus_sec"]
                break
            lines.append(f"{svc} still DOWN (since T+{since // 60})")
    return "\n".join(lines) or "no changes since last cycle"
