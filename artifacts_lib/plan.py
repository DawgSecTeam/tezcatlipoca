"""Target planning: what a run's artifacts are and where each comes from (pure, no I/O)."""

from pathlib import Path

from .constants import SIDES, SKIPPED, UNRECOVERABLE
from .manifest import resolve_recorded


def plan_targets(manifest, *, comp_dir=None):
    """What this run's artifacts are, and where each one comes from.

    Pure: no I/O, no transport. The result is the collector's input, and the CLI can print it to
    answer "why is RED-TEAM.md missing?" without touching a VM.

    Severity is derived from the manifest rather than from a per-file `required` flag: if the
    manifest says a red agent was present, a missing red report is worth shouting about; if it
    does not, the same absence is simply the truth about a run that had no red agent."""
    agents = manifest.get("agents") or {}
    paths = manifest.get("paths") or {}
    targets = []

    red = agents.get("red") or {}
    if red.get("present"):
        targets.append({
            "name": "red01", "route": "scp-jump", "kind": "remote",
            "node": red.get("node"), "vmid": red.get("vmid"), "ssh": red.get("ssh") or {},
            "note": ("bad-auto's VM, not a tezcatlipoca box — `badauto destroy` removes it, so a "
                     "missing red report after a normal harness run is expected unless the "
                     "harness or teardown collected before that"),
            "want": [
                # The sudo-staged copies: the originals are 0600 root, and one
                # unreadable file used to make the collector give up on the whole
                # red target (taking world.json with it).
                {"remote": "/tmp/ba/report-*.md", "to": "evidence/red/",
                 "canonical": SIDES["red"]},
                {"remote": "/tmp/ba/events.jsonl",
                 "local": "evidence/red/events.jsonl"},
                {"remote": "/tmp/ba/world.json", "local": "evidence/red/world.json"},
                {"remote": "/tmp/ba/report-secrets.md",
                 "local": "evidence/red/report-secrets.md"},
                {"cmd": "sudo -n journalctl -u bad-auto --no-pager",
                 "local": "evidence/red/bad-auto-journal.log"},
            ],
        })
    else:
        targets.append({"name": "red01", "route": "scp-jump", "kind": "remote", "want": [],
                        "status": SKIPPED, "note": "no red agent in this run"})

    run_dir = resolve_recorded(paths.get("run_dir"), comp_dir)
    harness_want = [{"local_name": name} for name in
                    ("run.json", "T0.txt", "monitor.log", "watchdog.log", "scoreboard-state.jsonl",
                     "INTERACTION.md", "deploy.log", "evidence/alerts.jsonl",
                     "evidence/final-scoreboard.json", ".deploy-timings.jsonl")]
    if run_dir and Path(run_dir).is_dir():
        targets.append({"name": "harness", "route": "local", "kind": "local",
                        "root": str(run_dir), "to": "evidence/harness/",
                        "note": "the scrim harness run directory", "want": harness_want})
    else:
        targets.append({"name": "harness", "route": "local", "kind": "local", "root": run_dir,
                        "status": SKIPPED if not run_dir else UNRECOVERABLE, "want": [],
                        "note": ("no run dir recorded in the manifest" if not run_dir
                                 else f"recorded run dir {run_dir} no longer exists")})

    blue = agents.get("blue") or {}
    workdirs = [d for d in (resolve_recorded(w, comp_dir)
                            for w in (paths.get("blue_workdirs") or []))
                if d and d.is_dir()]
    if blue.get("present") and workdirs:
        for workdir in workdirs:
            name = Path(workdir).name
            targets.append({
                "name": f"blue:{name}", "route": "local", "kind": "local",
                "root": str(workdir), "to": f"evidence/blue/{name}/",
                "note": "blue agent workdir (operator host, not a guest box)",
                "want": [
                    {"local_name": "REPORT.md", "canonical": SIDES["blue"]},
                    {"local_name": "FINDINGS.md"},
                    {"local_name": "LOG.md"}, {"local_name": "NOTEBOOK.md"},
                    {"local_name": "feed.log"},
                ],
                "want_globs": [{"pattern": "sub*.md"}, {"pattern": "sub*.txt"},
                               {"pattern": "cycles/*"}, {"pattern": "submissions/*"}],
            })
    else:
        targets.append({"name": "blue", "route": "local", "kind": "local", "want": [],
                        "status": SKIPPED,
                        "note": ("no blue agent in this run" if not blue.get("present")
                                 else "blue agent present but no workdir was recorded")})

    if comp_dir:
        targets.append({"name": "comp", "route": "local", "kind": "local",
                        "root": str(comp_dir), "to": "evidence/harness/",
                        "note": "per-competition deploy sidecar",
                        "want": [{"local_name": ".deploy-timings.jsonl"}],
                        # Saved by portal_ops.teardown_portal when the comp ran the
                        # student portal; a glob, so comps without one record nothing.
                        "want_globs": [{"pattern": "portal-access.log"}]})

    engine = resolve_recorded(paths.get("engine_evidence"), comp_dir)
    if engine and Path(engine).is_dir():
        targets.append({"name": "engine", "route": "local", "kind": "local", "root": str(engine),
                        "to": "evidence/engine/",
                        "note": "engine-side capture (scoreboard, injects, services)",
                        "want": [], "want_globs": [{"pattern": "*"}]})
    else:
        targets.append({"name": "engine", "route": "local", "kind": "local", "root": engine,
                        "want": [], "status": SKIPPED,
                        "note": ("no engine capture exists (the harness captures it before the "
                                 "scoring DB is destroyed)")})
    return targets


def want_label(want):
    """A stable id for a wanted item — the key the second teardown run matches on."""
    return (want.get("remote") or want.get("local_name") or want.get("cmd")
            or want.get("pattern"))
