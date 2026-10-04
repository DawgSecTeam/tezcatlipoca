"""Per-competition injects: load, anchor to wall-clock, fingerprint for the phase-7 marker."""

import json


def load_injects(comp_dir):
    """Load per-competition injects (title/description/offsets/attachments); [] when no injects/ dir."""
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return []

    injects = []
    for sub in sorted(injects_dir.iterdir()):
        manifest = sub / "inject.json"
        if not sub.is_dir() or not manifest.exists():
            continue
        meta = json.loads(manifest.read_text())

        description = meta.get("description", "")
        if meta.get("description_file"):
            desc_path = sub / meta["description_file"]
            if desc_path.exists():
                description = desc_path.read_text()

        skip = {"inject.json", meta.get("description_file")}
        files = [str(f) for f in sorted(sub.iterdir()) if f.is_file() and f.name not in skip]

        injects.append({
            "title":       meta["title"],
            "description": description,
            "open_offset_min":  meta.get("open_offset_min", 0),
            "due_offset_min":  meta.get("due_offset_min", 60),
            "close_offset_min": meta.get("close_offset_min", 90),
            "files":       files,
        })
    return injects


def resolve_inject_times(injects):
    """Resolve inject offsets to RFC3339 timestamps anchored at now (phase 7). Mutates in place."""

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    def rfc3339(minutes):
        return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for inj in injects:
        inj["open_time"] = rfc3339(inj.pop("open_offset_min", 0))
        inj["due_time"] = rfc3339(inj.pop("due_offset_min", 60))
        inj["close_time"] = rfc3339(inj.pop("close_offset_min", 90))
    return injects


def injects_fingerprint(injects):
    """Stable identity of the inject *definitions*, for phase 7's done-marker.

    Call this BEFORE `resolve_inject_times` — that function pops the offset fields and
    rewrites them as absolute timestamps, so a fingerprint taken afterwards changes on
    every run and would re-create the whole set every time.

    `injects_created` used to be a bare boolean, which meant an inject added (or a
    window edited) after the first phase-7 run was silently skipped forever on resume:
    `create_injects` dedups on titles precisely so a re-run is safe, but nothing ever
    re-ran it. Titles are what that dedup keys on, so they lead the fingerprint; the
    offsets and attachment names are included so a changed window or a swapped
    attachment is not missed either.
    """
    import hashlib

    items = []
    for inj in injects or []:
        items.append({
            "title": inj.get("title"),
            "open_offset_min": inj.get("open_offset_min", 0),
            "due_offset_min": inj.get("due_offset_min", 60),
            "close_offset_min": inj.get("close_offset_min", 90),
            "attachments": sorted(
                (a.get("name") if isinstance(a, dict) else str(a)) or ""
                for a in (inj.get("attachments") or [])),
        })
    blob = json.dumps(items, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
