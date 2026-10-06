"""The machine verdict: parsed from scrim-report.py's INTERACTION.md."""

import re
import subprocess
from pathlib import Path

from .constants import REPO
from .env import iso
from .hashing import seal_local_file
from .manifest import load_manifest, resolve_recorded, update_manifest

_VERDICT_SECTION = re.compile(r"^## Verdict\s*$(.*?)(?=^## |\Z)", re.M | re.S)


def ingest_verdict(test_path, *, source="evidence/harness/INTERACTION.md"):
    """Fold scrim-report.py's verdict into test.json's machine verdict.

    Parsed rather than recomputed on purpose: scrim-report.py owns the interaction score and the
    docs/rehearsal-gates.md gate table, and a second implementation here would drift from it.
    Tolerant of a missing/partial file (returns {} and leaves the manifest alone) because
    INTERACTION.md is produced by a best-effort step."""
    test_path = Path(test_path)
    path = test_path / source
    if not path.exists():
        return {}
    text = path.read_text()
    verdict_section = _VERDICT_SECTION.search(text)
    section = verdict_section.group(1) if verdict_section else text
    status = re.search(r"\*\*(GREEN|NOT READY|FAILED RUN|NO INTERACTION)", section)
    if not status:
        return {}
    score = re.search(r"interaction score (\d+)", section)
    gates = {"pass": 0, "fail": 0, "n/a": 0}
    gates_section = re.search(r"^## Gates.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if gates_section:
        for line in gates_section.group(1).splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5 or cells[0] in ("side", "---"):
                continue
            cell = cells[-1].lower()
            if cell in ("pass", "fail", "n/a"):
                gates[cell] += 1
    verdict = {"status": status.group(1), "score": int(score.group(1)) if score else None,
               "gates_passed": gates["pass"], "gates_failed": gates["fail"],
               "gates_na": gates["n/a"], "source": source, "at": iso()}
    update_manifest(test_path, verdict=verdict)
    return verdict


def verdict_block(test_path):
    """The machine verdict, lifted verbatim from INTERACTION.md's `## Verdict` section."""
    interaction = Path(test_path) / "evidence" / "harness" / "INTERACTION.md"
    if not interaction.exists():
        return None
    match = _VERDICT_SECTION.search(interaction.read_text())
    block = (match.group(1).strip() if match else "")
    return block or None


def ensure_verdict(test_path, *, echo=print, dry_run=False):
    """Run scrim-report.py when it has not run, then fold its verdict into the manifest.

    Best-effort throughout: a missing verdict downgrades REPORT.md to "no machine verdict was
    captured", which is honest, while a raise here would cost the operator a teardown."""
    test_path = Path(test_path)
    interaction = test_path / "evidence" / "harness" / "INTERACTION.md"
    run_dir = resolve_recorded((load_manifest(test_path).get("paths") or {}).get("run_dir"),
                                test_path.parent.parent)
    if not interaction.exists() and run_dir and Path(run_dir).is_dir() and not dry_run:
        try:
            proc = subprocess.run(["python3", "scrim-report.py", str(run_dir)], cwd=REPO,
                                  capture_output=True, text=True, timeout=300)
            produced = Path(run_dir) / "INTERACTION.md"
            if proc.returncode == 0 and produced.exists():
                seal_local_file(produced, interaction)
            else:
                echo(f"  WARNING: scrim-report.py exited {proc.returncode} — no machine verdict "
                     f"({(proc.stderr or '').strip()[:120]})")
        except (OSError, subprocess.SubprocessError) as e:
            echo(f"  WARNING: could not run scrim-report.py ({type(e).__name__}: {e}) — no "
                 "machine verdict")
    return ingest_verdict(test_path)
