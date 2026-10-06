"""The canonical documents: honest stubs and the REPORT.md skeleton."""

import json
from pathlib import Path

from config_ops import write_text_atomic

from .collect import load_collection
from .constants import LOST, NOT_COLLECTED, REPORT_NAME, SIDES, SKIPPED, TODO_MARK, UNRECOVERABLE
from .env import iso
from .manifest import load_manifest
from .status import side_status
from .verdict import verdict_block


def write_stub(test_path, side, status, reason, *, manifest=None):
    """Write the honest placeholder for a canonical document we could not obtain.

    An absent file is indistinguishable from a wiring bug; a stub naming the status and the
    reason is not. That is the whole reason the three documents have fixed filenames."""
    manifest = manifest or load_manifest(test_path)
    filename = SIDES[side]
    run = manifest.get("run_id") or manifest.get("key") or "unknown-run"
    body = ["---", f"id: {run}", f"side: {side}", "generated_by: artifacts_ops.py",
            f"status: {status}", f"generated: {iso()}", "---", "",
            f"# {filename} — not available ({status})", ""]
    if status == SKIPPED:
        body += [f"This run had no {side} agent, so no {side} report exists. That is the truth "
                 "about the run, not a collection failure.", ""]
    elif status == NOT_COLLECTED:
        body += [f"The {side} report was never collected — the evidence it would come from is "
                 "still where the run left it, so this is recoverable. Collect it with:",
                 "",
                 f"```\npython3 test-artifacts.py collect "
                 f"{manifest.get('comp')} {manifest.get('key')}\n```", ""]
    elif status == UNRECOVERABLE:
        body += [f"The {side} source was already destroyed when artifacts were collected, so "
                 "nothing could be read from it. Evidence for that side is unrecoverable.", ""]
    else:
        body += [f"Collection could not obtain a {side} report.", ""]
    body += [f"- reason: {reason}", f"- competition: {manifest.get('comp')}",
             f"- key: {manifest.get('key')}", "",
             "Whatever evidence was collected is under `evidence/`; `collection.json` records "
             "exactly what was and was not obtained."]
    path = Path(test_path) / filename
    write_text_atomic(path, "\n".join(body) + "\n", mode=0o600)
    return path


def timing_rows(test_path):
    """(per-phase totals, slowest ops) from the timing sidecar, when it was captured."""
    path = Path(test_path) / "evidence" / "harness" / ".deploy-timings.jsonl"
    if not path.exists():
        path = Path(test_path) / "evidence" / "harness" / "deploy-timings.jsonl"
    if not path.exists():
        return [], []
    totals, slowest = {}, []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        phase = row.get("phase") or "?"
        seconds = float(row.get("seconds") or 0.0)
        totals[phase] = totals.get(phase, 0.0) + seconds
        slowest.append((seconds, phase, row.get("op"), row.get("target")))
    slowest.sort(reverse=True)
    return sorted(totals.items(), key=lambda kv: -kv[1]), slowest[:5]


def render_report_skeleton(test_path):
    """The synthesis document, with every machine section filled and judgement marked.

    Deliberately does NOT invent a success metric: the interaction score and the
    docs/rehearsal-gates.md table are the definition (scrim-report.py evaluates them), and a
    second opinion computed here would drift from them."""
    test_path = Path(test_path)
    manifest = load_manifest(test_path)
    collection = load_collection(test_path)
    run = manifest.get("run_id") or manifest.get("key")
    verdict = manifest.get("verdict") or {}
    lines = ["---", f"id: {run}", f"kind: {manifest.get('kind')}",
             f"comp: {manifest.get('comp')}", f"generated: {iso()}",
             "generated_by: artifacts_ops.py",
             "author: null   # fill the judgement sections, then: test-artifacts.py verify --seal", "---",
             "", f"# {manifest.get('kind', 'run')} report — {manifest.get('comp')} — {run}", "",
             "## Verdict", ""]
    if verdict.get("status"):
        detail = []
        if verdict.get("score") is not None:
            detail.append(f"interaction score {verdict['score']}")
        if verdict.get("gates_passed") is not None:
            detail.append(f"gates {verdict['gates_passed']} pass / "
                          f"{verdict.get('gates_failed')} fail / {verdict.get('gates_na')} n/a")
        lines.append(f"**{verdict['status']}**"
                     + (" — " + "; ".join(detail) if detail else "")
                     + f" (source: {verdict.get('source', 'evidence/harness/INTERACTION.md')})")
    else:
        block = verdict_block(test_path)
        if block:
            lines.append(block)
        else:
            lines += ["_No machine verdict was captured for this run "
                      "(no INTERACTION.md in its evidence)._",
                      "", "<!-- machine: do not invent a verdict; say what was and was not "
                      "measured. -->"]
    lines += ["", "## Environment", "", "| field | value |", "|---|---|"]
    created_by = manifest.get("created_by") or {}
    for label, value in (
            ("competition", manifest.get("comp_name") or manifest.get("comp")),
            ("run id", manifest.get("run_id") or "(none — no deploy state)"),
            ("kind", manifest.get("kind")),
            ("node(s)", ", ".join(manifest.get("nodes") or []) or "?"),
            ("endpoint", manifest.get("endpoint")),
            ("teams", manifest.get("teams")),
            ("boxes", ", ".join(manifest.get("boxes") or []) or "?"),
            ("worktree", created_by.get("worktree")),
            ("git rev", created_by.get("git_rev")),
            ("created", manifest.get("created_at"))):
        lines.append(f"| {label} | {value if value is not None else '?'} |")
    agents = manifest.get("agents") or {}
    red, blue = agents.get("red") or {}, agents.get("blue") or {}
    lines += ["", "Agents: red "
              + (f"present ({red.get('ip') or '?'}" + (f", vmid {red['vmid']}"
                 if red.get("vmid") else "") + ")" if red.get("present") else "absent")
              + "; blue " + (f"present ({blue.get('teams') or '?'} team(s))"
                             if blue.get("present") else "absent") + "."]

    lines += ["", "## Timeline and cost", ""]
    phases = manifest.get("phases") or []
    if phases:
        lines += ["| phase | at |", "|---|---|"]
        lines += [f"| {p.get('phase')} | {p.get('at')} |" for p in phases]
    else:
        lines.append("_No phase markers recorded._")
    phase_rows, slowest = timing_rows(test_path)
    if phase_rows:
        lines += ["", "Where the wall clock went (timing sidecar):", "",
                  "| phase | seconds |", "|---|---|"]
        lines += [f"| {phase} | {seconds:.0f} |" for phase, seconds in phase_rows]
        lines += ["", "Slowest operations:", ""]
        lines += [f"- {seconds:.0f}s — {phase} / {op} ({target or '-'})"
                  for seconds, phase, op, target in slowest]

    lines += ["", "## Incidents", "",
              "<!-- machine seeds these from alerts, degradations, verify failures and collection "
              "failures; an author must classify each and add what only a human saw. -->", "",
              "| when | what | impact | resolved | evidence |", "|---|---|---|---|---|"]
    alerts = Path(test_path) / "evidence" / "harness" / "evidence" / "alerts.jsonl"
    if not alerts.exists():
        alerts = Path(test_path) / "evidence" / "harness" / "alerts.jsonl"
    if alerts.exists():
        counts = {}
        for line in alerts.read_text().splitlines():
            try:
                kind = json.loads(line).get("kind") or "?"
            except ValueError:
                continue
            counts[kind] = counts.get(kind, 0) + 1
        for kind, count in sorted(counts.items()):
            lines.append(f"| (run) | alert `{kind}` x{count} | ? | ? | "
                         "`evidence/harness/alerts.jsonl` |")
    for entry in manifest.get("degradations") or []:
        lines.append(f"| (run) | degraded: {entry.get('what')} — {entry.get('detail')} | ? | ? | "
                     "`test.json` degradations |")
    for target in collection.get("targets", []):
        for item in target.get("files", []):
            if item.get("status") in LOST:
                lines.append(f"| (collection) | {target['name']}: {item.get('want')} "
                             f"{item['status']} | artifact missing | no | `collection.json` |")
    verify = manifest.get("verify") or {}
    if verify.get("exit_code") not in (None, 0):
        lines.append(f"| (verify) | verify-competition.py exit {verify['exit_code']} | gate "
                     f"failure | ? | `{verify.get('log', 'evidence/harness/')}` |")
    lines += ["", "<!-- TODO(author): classify the seeded rows, delete the ones that are not "
              "incidents, add anything only a human saw -->", ""]

    lines += ["## Red", ""]
    if red.get("present"):
        lines.append(f"Report: `{SIDES['red']}` ({side_status(collection, 'red')}). Red metrics "
                     "live in the Verdict section; this section is the narrative.")
        lines.append("")
        lines.append("<!-- TODO(author): what red achieved, what it could not, and why -->")
    else:
        lines.append("No red agent took part in this run.")
    lines += ["", "## Blue", ""]
    if blue.get("present"):
        lines.append(f"Report: `{SIDES['blue']}` ({side_status(collection, 'blue')}). Sealed "
                     "evidence (logs, cycles, submissions) is under `evidence/blue/`.")
        lines.append("")
        lines.append("<!-- TODO(author): what blue did, time-to-restore, inject outcomes, and "
                     "whether the defence was real or nominal -->")
    else:
        lines.append("No blue agent took part in this run.")

    lines += ["", f"## Recommendations — this competition "
              f"({manifest.get('comp_name') or manifest.get('comp')})", "",
              "<!-- TODO(author): what should change about THIS competition — scenario, pins, "
              "lineup, difficulty, packet, scoring -->", "",
              "## Recommendations — tezcatlipoca", "",
              "<!-- TODO(author): tool defects and gaps this run exposed. The fixed heading is "
              "the harvest path into docs/known-issues.md and a fixes plan. One bullet each: "
              "symptom -> evidence path -> suggested owner/file. -->", "",
              "## Not verified / caveats", ""]
    limitations = []
    for target in collection.get("targets", []):
        if target.get("status") == SKIPPED:
            limitations.append(f"{target['name']}: skipped ({target.get('reason') or 'n/a'})")
        elif target.get("status") in LOST:
            limitations.append(f"{target['name']}: {target['status']}")
    if verdict.get("gates_na"):
        limitations.append(f"{verdict['gates_na']} gate(s) n/a (no data recorded for them)")
    lines += [f"- {item}" for item in limitations] or ["- (none recorded)"]
    lines.append("")
    return "\n".join(lines)


def write_report_skeleton(test_path, *, force=False):
    """Write REPORT.md, never clobbering an authored one.

    A second teardown run must not destroy the write-up somebody did after the first: that would
    teach people to avoid re-running teardown, which is the one thing they must be willing to do
    (destroy-competition.py's own failure path says "re-run this command")."""
    test_path = Path(test_path)
    path = test_path / REPORT_NAME
    manifest = load_manifest(test_path)
    if path.exists() and not force:
        if (manifest.get("writeup") or {}).get("status") != "needs-writeup":
            return path
        if TODO_MARK not in path.read_text():
            return path  # an author has started (or finished) filling it in
    write_text_atomic(path, render_report_skeleton(test_path), mode=0o600)
    return path
