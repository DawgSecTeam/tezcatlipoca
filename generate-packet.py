#!/usr/bin/env python3
"""Generate the competition-wide competitor packet (pure local-file -> Markdown render)."""

import argparse
import json
import sys
from pathlib import Path

from common.constants import WINDOWS_ADMIN_USER
from common.utils import (compfile_flag, compfile_value, is_unmanaged, load_compfile,
                          load_users_config)
from common.windows_ops import is_windows_template

REPO_ROOT = Path(__file__).resolve().parent


def _service_display(pin):
    """The scoreboard display name Quotient will use — one resolution, shared with setup.py."""
    from quotient.setup import _resolve_pin
    _name, check_key, cfg = _resolve_pin(pin)
    if check_key is None:
        return None
    return cfg["Display"]


def _scored_services(pins):
    """Only pins that produce a scored check: a `plant_only` (scored: false) pin is not one."""
    out = set()
    for pin in pins or []:
        if isinstance(pin, dict) and pin.get("plant_only"):
            continue
        display = _service_display(pin)
        if display:
            out.add(display)
    return out


def load_inject_schedule(comp_dir):
# Inject titles + timing offsets only (never prompt.md content).
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return []
    schedule = []
    for sub in sorted(injects_dir.iterdir()):
        manifest = sub / "inject.json"
        if not sub.is_dir() or not manifest.exists():
            continue
        meta = json.loads(manifest.read_text())
        schedule.append({
            "title": meta.get("title", sub.name),
            "open_offset_min": meta.get("open_offset_min", 0),
            "due_offset_min": meta.get("due_offset_min", 60),
            "close_offset_min": meta.get("close_offset_min", 90),
        })
    return schedule


def _fmt_offset(minutes):
    hours, mins = divmod(minutes, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def build_packet(comp_dir):
    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    box_username, _credlist_usernames = load_users_config(comp_dir)

    boxes_path = comp_dir / "boxes.json"
    if not boxes_path.exists():
        raise SystemExit(
            f"ERROR: {boxes_path} not found. Run create-competition.py far enough to generate "
            f"it, or hand-author it — see docs/usage-agents.md#pre-authoring-a-competition."
        )
    boxes = json.loads(boxes_path.read_text())

    services_path = comp_dir / "box_services.json"
    if not services_path.exists():
        raise SystemExit(
            f"ERROR: {services_path} not found. Run create-competition.py far enough to "
            f"generate it, or hand-author it — see "
            f"docs/usage-agents.md#pinning-a-competitions-configuration. (box_vulns.json is "
            f"deliberately never read here — planted misconfigs stay out of the packet.)"
        )
    box_services = json.loads(services_path.read_text())

    lines = []
    lines.append(f"# {name} — Competitor Packet")
    lines.append("")
    lines.append(f"**Difficulty:** {difficulty} / 10")
    lines.append("")
    lines.append("## Scenario")
    lines.append("")
    lines.append(scenario or "_(no scenario text set)_")
    lines.append("")

    lines.append("## Format")
    lines.append("")
    lines.append(
        "This is an availability-focused defense competition. Each of your team's boxes is "
        "polled periodically for the services listed below; keeping them up and reachable "
        "scores points over time. Full credentials (team login, box login, and the accounts "
        "the scoring checks authenticate with) are issued separately at competition start — "
        "this packet only covers what you can prepare for in advance."
    )
    lines.append("")

    schedule = load_inject_schedule(comp_dir)
    if schedule:
        lines.append("### Injects")
        lines.append("")
        lines.append(
            "In addition to uptime scoring, you'll receive timed written taskings (\"injects\") "
            "during the event. Content is released when each one opens; only the schedule is "
            "known ahead of time:"
        )
        lines.append("")
        lines.append("| # | Title | Opens | Due | Closes |")
        lines.append("|---|---|---|---|---|")
        for i, inj in enumerate(schedule, 1):
            lines.append(
                f"| {i} | {inj['title']} | {_fmt_offset(inj['open_offset_min'])} "
                f"| {_fmt_offset(inj['due_offset_min'])} | {_fmt_offset(inj['close_offset_min'])} |"
            )
        lines.append("")
        lines.append("_Offsets are relative to competition start._")
        lines.append("")

    lines.append("## Network layout")
    lines.append("")
    lines.append(
        "Your team is assigned one `/24` subnet, `192.168.<your-team-octet>.0/24` — the same "
        "layout for every team, isolated from every other team's. Your systems:"
    )
    lines.append("")
    lines.append("| Hostname | Address | Template/Role |")
    lines.append("|---|---|---|")
    for b in boxes:
        lines.append(f"| {b['name']} | 192.168.X.{b['last_octet']} | {b.get('template', '—')} |")
    lines.append("")
    lines.append("_(`X` = your team's assigned octet, given to you at competition start.)_")
    lines.append("")

    lines.append("## Services")
    lines.append("")
    lines.append("Publicly reachable, scored services on each box:")
    lines.append("")
    lines.append("| Hostname | Services |")
    lines.append("|---|---|")
    for b in boxes:
        displays = sorted(_scored_services(box_services.get(b["name"], [])))
        display = ", ".join(displays) if displays else "—"
        lines.append(f"| {b['name']} | {display} |")
    lines.append("")

    lines.append("## System access")
    lines.append("")
    linux_boxes = [b["name"] for b in boxes
                   if not is_unmanaged(b) and not is_windows_template(b.get("template", ""))]
    windows_boxes = [b["name"] for b in boxes
                     if not is_unmanaged(b) and is_windows_template(b.get("template", ""))]
    if linux_boxes:
        lines.append(
            f"Linux box login account: **`{box_username}`** "
            f"({', '.join(linux_boxes)}).")
    if windows_boxes:
        lines.append(
            f"Windows box login account: **`{WINDOWS_ADMIN_USER}`** "
            f"({', '.join(windows_boxes)}).")
    lines.append(
        "Their passwords, and the separate accounts the scoring checks authenticate with, "
        "are issued at competition start — not in this packet."
    )
    lines.append("")
    if compfile_flag(comp_dir / "Compfile", "portal", 0):
        # Student portal (docs/portal.md): Quotient team login -> Proxmox web console.
        where = compfile_value(comp_dir / "Compfile", "portal_hostname", "")
        lines.append("### Web console")
        lines.append("")
        lines.append(
            (f"Reach your boxes through the range portal at **https://{where}**. "
             if where else
             "Reach your boxes through the range portal (its address is announced at "
             "competition start). ")
            + "Sign in with your team's scoreboard (Quotient) login: the portal lists your "
            "team's boxes, and each one has a **Console** button that opens its screen in "
            "the browser. Log in at the console with the box credentials above. Consoles "
            "unlock when the competition starts."
        )
        lines.append("")
        lines.append(
            "The console is the machine's own screen, so it keeps working even if you "
            "firewall the box or break its network. There is no clipboard: use the "
            "console's **Type text…** button to send text as keystrokes."
        )
        lines.append("")

    lines.append("## Rules of engagement")
    lines.append("")
    lines.append("- Keep your assigned services up and reachable — that's what's scored.")
    lines.append(
        "- Don't attack, scan, or otherwise interfere with the scoring engine or any "
        "infrastructure outside your team's subnet."
    )
    lines.append(
        "- Don't change the password of any account the scoring checks authenticate with "
        "(see System access above) — doing so takes that check down for the rest of the event."
    )
    lines.append("- Direct questions to the event organizers through the announced channel.")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate the competitor-facing packet (network/system/services layout) "
                    "for a competition. No live infrastructure required."
    )
    parser.add_argument("comp_dir", help="path to competitions/<id>")
    args = parser.parse_args()

    comp_dir = Path(args.comp_dir).resolve()
    if not comp_dir.is_dir():
        print(f"ERROR: competition directory not found: {comp_dir}", file=sys.stderr)
        return 2

    packet = build_packet(comp_dir)
    out_path = comp_dir / "packet.md"
    out_path.write_text(packet)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
