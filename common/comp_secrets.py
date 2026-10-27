"""Per-COMPETITION secrets that must outlive a per-run workspace.

`box_password` is a golden-hash input: re-minting it rebuilds every golden. The workspace's
state document is per-RUN (the dashboard seeds it empty), so a fresh run of a competition
forgot it and rebuilt every golden the kept templates exist to avoid. This cache holds it
per competition on the run host; a full teardown forgets it.
"""

import json
import re
from pathlib import Path

from config.config_ops import write_text_atomic

CACHE_DIR = Path.home() / ".tezcatlipoca" / "comp-secrets"


def slug(comp_name):
    return re.sub(r"[^a-z0-9-]+", "-", str(comp_name).lower()).strip("-")


def path(comp_name):
    return CACHE_DIR / f"{slug(comp_name)}.json"


def load(comp_name):
    if not comp_name:
        return None
    try:
        return json.loads(path(comp_name).read_text(encoding="utf-8")).get("box_password")
    except (OSError, ValueError):
        return None


def store(comp_name, box_password):
    if not comp_name:
        return
    target = path(comp_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, json.dumps({"box_password": box_password}) + "\n")


def forget(comp_name):
    if not comp_name:
        return
    path(comp_name).unlink(missing_ok=True)
