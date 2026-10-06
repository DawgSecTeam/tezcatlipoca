"""Resolve --teams/--boxes/--platform selections into the target list."""

import pipeline_api

from range_ops import load_targets


def parse_team_selector(raw, teams):
    """Parse team selector (team key, number, or subnet identifier)."""
    by_key = {k.lower(): k for k in teams}
    by_ident = {str(v["identifier"]): k for k, v in teams.items()}
    by_number = {k.lower().removeprefix("team"): k for k in teams}

    selected = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        low = token.lower()
        match = by_key.get(low) or by_ident.get(low) or by_number.get(low)
        if match is None:
            raise SystemExit(
                f"  ERROR: no team matches '{token}'. This competition has: "
                + ", ".join(f"{k} (identifier {v['identifier']})" for k, v in teams.items())
            )
        if match not in selected:
            selected.append(match)
    return selected


def parse_box_selector(raw, boxes):
    known = {b["name"].lower(): b["name"] for b in boxes}
    selected = []
    for token in (t.strip() for t in raw.split(",") if t.strip()):
        match = known.get(token.lower())
        if match is None:
            raise SystemExit(
                f"  ERROR: no box named '{token}'. This competition has: "
                + ", ".join(b["name"] for b in boxes)
            )
        if match not in selected:
            selected.append(match)
    return selected


def box_platform(box):
    """Platform via pipeline_api.os_to_platform (the single map nakon also uses)."""
    return pipeline_api.os_to_platform(box.get("template", ""))


def select_targets(comp_dir, teams, boxes, args):
    """Full target list, narrowed by whichever filters were given (AND-combined)."""
    targets = load_targets(comp_dir, teams, boxes)

    if args.teams:
        keep = set(parse_team_selector(args.teams, teams))
        targets = [t for t in targets if t["team_key"] in keep]
    if args.boxes:
        keep = set(parse_box_selector(args.boxes, boxes))
        targets = [t for t in targets if t["box_name"] in keep]
    if args.platform:
        want = args.platform.lower()
        targets = [t for t in targets if box_platform(t["box"]) == want]

    return targets
