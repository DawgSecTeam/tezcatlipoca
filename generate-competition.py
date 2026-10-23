#!/usr/bin/env python3
"""Author a competition from one JSON spec: boxes, OSes, services, optional vulns."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from common.constants import MAX_BOXES_PER_TEAM
from common.utils import BOX_USERNAME_DEFAULT, CREDLIST_USERNAMES_DEFAULT

OS_TEMPLATES = {
    "debian": "base-debian13-lite-fix",
    "ubuntu": "base-ubuntu24.04-fix",
    "windows": "base-windows-server",
    "win": "base-windows-server",
    "fedora": "base-fedora44-fix",
}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class SpecError(Exception):
    pass


def _box(raw, index):
    if not isinstance(raw, dict):
        raise SpecError(f"boxes[{index}] must be an object")
    name = str(raw.get("name") or f"box{index + 1:02d}")
    if not NAME_RE.match(name):
        raise SpecError(f"boxes[{index}] name {name!r}: use lowercase letters, digits, '.', '_' or '-'")
    os_name = str(raw.get("os") or "").strip().lower()
    template = raw.get("template") or OS_TEMPLATES.get(os_name)
    if not template:
        raise SpecError(f"box {name!r}: set os to one of {sorted(OS_TEMPLATES)} or give template")
    services = raw.get("services")
    if not isinstance(services, list):
        raise SpecError(f"box {name!r}: services must be a list")
    for service in services:
        if isinstance(service, str):
            continue
        if not isinstance(service, dict) or not service.get("name"):
            raise SpecError(f"box {name!r}: every service needs a name")
    box = {"name": name, "last_octet": int(raw.get("last_octet", index + 2)),
           "cpu": int(raw.get("cpu", 1)), "memory_mb": int(raw.get("memory_mb", 2048)),
           "disk_gb": raw.get("disk_gb"), "template": template,
           "services": services,
           "vulns": raw.get("vulns") or []}
    for key in ("disk_iface", "unmanaged", "in_path"):
        if raw.get(key) not in (None, ""):
            box[key] = raw[key]
    return box


def build(spec):
    if not isinstance(spec, dict):
        raise SpecError("the spec must be a JSON object")
    name = str(spec.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        raise SpecError("name is required: use lowercase letters, digits, '.', '_' or '-'")
    raw_boxes = spec.get("boxes")
    if not isinstance(raw_boxes, list):
        raise SpecError("boxes must be a list")
    expanded = []
    for raw in raw_boxes:
        if not isinstance(raw, dict):
            raise SpecError("each box entry must be an object")
        count = int(raw.get("count", 1))
        if count < 1:
            raise SpecError("box count must be at least 1")
        for n in range(count):
            item = {k: v for k, v in raw.items() if k != "count"}
            if count > 1 and raw.get("name"):
                item["name"] = f"{raw['name']}{n + 1:02d}"
            expanded.append(item)
    if not 1 <= len(expanded) <= MAX_BOXES_PER_TEAM:
        raise SpecError(f"boxes must expand to 1..{MAX_BOXES_PER_TEAM} entries")
    boxes = [_box(raw, i) for i, raw in enumerate(expanded)]
    names = [b["name"] for b in boxes]
    if len(set(names)) != len(names):
        raise SpecError(f"duplicate box name(s): {sorted({n for n in names if names.count(n) > 1})}")
    octets = [b["last_octet"] for b in boxes]
    if len(set(octets)) != len(octets):
        raise SpecError(f"duplicate last_octet(s): {sorted({o for o in octets if octets.count(o) > 1})}")
    if any(not 2 <= o <= 254 for o in octets):
        raise SpecError("last_octet must be 2..254 (.1 is the team gateway)")
    vulns = spec.get("vulns") or {}
    if not isinstance(vulns, dict):
        raise SpecError("vulns must be an object keyed by box name")
    for key in vulns:
        if key not in names:
            raise SpecError(f"vulns names unknown box {key!r}")
    users = {"box_username": spec.get("box_username") or BOX_USERNAME_DEFAULT,
             "credlist_usernames": spec.get("credlist_usernames") or list(CREDLIST_USERNAMES_DEFAULT)}
    if len(users["credlist_usernames"]) != 3:
        raise SpecError("credlist_usernames must contain exactly three names")
    compfile = {"name": name,
                "scenario": str(spec.get("scenario") or f"{name} practice range").replace("\n", " "),
                "difficulty": int(spec.get("difficulty", 1)),
                "remote_access": 0}
    compfile.update(spec.get("compfile") or {})
    return {"name": name, "boxes": boxes, "vulns": vulns, "users": users, "compfile": compfile}


def write(comp_dir, built, *, force=False):
    comp_dir = Path(comp_dir)
    if comp_dir.exists() and any(comp_dir.iterdir()) and not force:
        raise SpecError(f"{comp_dir} already exists and is not empty (use --force to overwrite)")
    comp_dir.mkdir(parents=True, exist_ok=True)
    (comp_dir / "injects").mkdir(exist_ok=True)
    (comp_dir / "Compfile").write_text(
        "".join(f"{key} {value}\n" for key, value in built["compfile"].items()), encoding="utf-8")
    boxes = [{k: v for k, v in box.items() if k not in ("services", "vulns")}
             for box in built["boxes"]]
    (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2) + "\n", encoding="utf-8")
    (comp_dir / "box_services.json").write_text(json.dumps(
        {box["name"]: box["services"] for box in built["boxes"]}, indent=2) + "\n", encoding="utf-8")
    (comp_dir / "box_vulns.json").write_text(json.dumps(
        {box["name"]: built["vulns"].get(box["name"], box["vulns"]) for box in built["boxes"]},
        indent=2) + "\n", encoding="utf-8")
    (comp_dir / "users.json").write_text(json.dumps(built["users"], indent=2) + "\n", encoding="utf-8")
    (comp_dir / "packet.md").write_text(
        f"# {built['name']} — Competitor Packet\n\n{built['compfile']['scenario']}\n", encoding="utf-8")
    return comp_dir


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("spec", help="JSON spec: name, boxes[{name,os,services}], optional vulns/compfile")
    p.add_argument("--comp-dir", help="output directory (default: $TEZ_COMPETITIONS_ROOT or ./competitions/<name>)")
    p.add_argument("--force", action="store_true", help="overwrite an existing non-empty comp dir")
    args = p.parse_args(argv)
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        built = build(spec)
        root = Path(os.environ.get("TEZ_COMPETITIONS_ROOT") or "competitions")
        comp_dir = Path(args.comp_dir) if args.comp_dir else root / built["name"]
        write(comp_dir, built, force=args.force)
    except (OSError, ValueError, SpecError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {comp_dir}")
    for box in built["boxes"]:
        print(f"  {box['name']:<10} {box['template']:<26} .{box['last_octet']:<3} "
              f"{len(box['services'])} service(s), {len(box['vulns'])} vuln(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
