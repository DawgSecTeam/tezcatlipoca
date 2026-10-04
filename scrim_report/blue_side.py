import re
from pathlib import Path


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
        m["injects"] += count_inject_submissions(wd)
    return m


# An inject deliverable the blue agent wrote. Accepted shapes, in the order they were
# observed: `sub-<id>.md` (the documented form), `sub.md` and `sub7.md` (what agents
# actually produced in the scale8 soak), and `sub-<label>.md` for a non-numeric id.
# Deliberately anchored on "sub" plus an optional short id, so an unrelated `submarine.md`
# or `submission-notes.md` in the team dir is not counted as a submitted inject.
_SUB_FILE_RE = re.compile(r"^sub(?:-?\d+|-[\w.-]+)?\.(?:md|txt)$", re.IGNORECASE)


def count_inject_submissions(team_dir):
    """How many inject deliverables this team produced.

    scale8-soak-2026-10-02 reported "injects submitted: 0" and scored the blue inject gate
    FAIL while the run had really submitted them: the teams wrote `sub.md`, `sub7.md` …
    `sub12.md` at the team directory root, and the counter only matched `sub-*.md` or files
    inside `submissions/`. A gate that reports zero because of a filename is worse than no
    gate — it fails a team for something it did.

    Both shapes count, and the `submissions/` directory still counts whatever is in it
    (agents that follow the documented path must not be penalised either)."""
    found = {p for p in team_dir.glob("sub*") if p.is_file() and _SUB_FILE_RE.match(p.name)}
    subs = team_dir / "submissions"
    if subs.is_dir():
        found |= {p for p in subs.iterdir() if p.is_file()}
    return len(found)
