import re
from pathlib import Path


def down_windows(snaps):
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
                    down_sec += tp - down_at
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
         "notebook_entries": 0, "eradication": 0, "evictions_logged": 0}
    # Artifact names are drawn per family (tlaloc/effects/artifactnames.py), so eradication
    # attribution matches behavior classes, not red's literals.
    erad_re = re.compile(
        r"(tznet|svc-netupdate|TzNet|red_key|authorized_keys|backdoor|rogue|"
        r"uid\s*=?\s*0|unauthorized|cron\.d|chattr|ld\.so\.preload|"
        r"schtasks|auto-start service|__EventFilter|attrib \+h|suid|"
        r"sshd_config\.d|profile\.d)", re.I)
    erad_verbs = re.compile(r"(removed|deleted|disabled|uninstalled|locked|changed.*back|reset)", re.I)
    for wd in sorted(Path(run_dir).glob("blue-team*")):
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
        # Red's health checks only see SSH-class footholds; the EVICTED: lines are the only
        # record of removed persistence, so without this real evictions score 0.
        m["evictions_logged"] += sum(1 for l in log_text.splitlines()
                                     if "EVICTED:" in l)
        m["injects"] += count_inject_submissions(wd)
    return m


# Anchored to sub+short-id so unrelated `submarine.md` / `submission-notes.md` don't count.
_SUB_FILE_RE = re.compile(r"^sub(?:-?\d+|-[\w.-]+)?\.(?:md|txt)$", re.IGNORECASE)


# Anything under submissions/ counts too: teams following the documented path must not lose work.
def count_inject_submissions(team_dir):
    found = {p for p in team_dir.glob("sub*") if p.is_file() and _SUB_FILE_RE.match(p.name)}
    subs = team_dir / "submissions"
    if subs.is_dir():
        found |= {p for p in subs.iterdir() if p.is_file()}
    return len(found)
