"""The competition's single estate state document (schema v2).

Two views exist and they are strictly separated:

* the **structured** sections (`roster`, `templates`, `placement`, `red`, `resources`,
  `teardown`, `external`) are owned by the module that writes them (`common.targets`,
  `engine.template_freeze`, `multinode.placement_record`, `red.red01_ops`, the inventory
  API here, `teardown`). They are never part of the flat view, so a deploy's flat save can
  no longer clobber them.
* the **flat** view (`flatten()`/`absorb()`) is the deploy's bookkeeping dict: identity,
  secrets, the flat template records, and the `run` catch-all. `absorb(replace=True)`
  replaces it wholesale, so a key popped from the flat dict is deleted from the document —
  the old append-only merge left wiped Phase-8 flags behind forever.
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from config.config_ops import write_text_atomic

SCHEMA_VERSION = 2
STATE_NAME = "estate-state.json"

_LIST_SECTIONS = {"resources"}
# `red` is written by two owners (red01_ops' identity, tlaloc_ops' run summary) and read
# through the flat view by teardown and the scrim harness, so it is merged, never replaced.
_MERGED_FLAT = ("red",)
# Derived identity: never deleted by an absorb that omits it.
_NEVER_DELETE = {"comp_name"}

# The deploy's flat keys and the section each lives in. Anything else lands in `run`.
_FLAT_SECTION = {
    "comp_name": "identity", "run_id": "identity", "scoring_vm_id": "identity",
    "engine_mgmt_ip": "identity", "engine_mgmt_gw": "identity", "endpoint": "identity",
    "node": "identity", "datastore": "identity", "name": "identity",
    "teams": "secrets", "admin_password": "secrets", "scoring_password": "secrets",
    "postgres_password": "secrets", "redis_password": "secrets", "box_password": "secrets",
    "box_creds": "secrets", "domain_creds": "secrets", "inject_password": "secrets",
    "engine_build_info": "templates", "engine_template_vmid": "templates",
    "engine_template_hash": "templates", "golden_template_ids": "templates",
    "golden_ids_by_slot": "templates", "golden_hashes": "templates",
    "boxes": "roster",
}
_FLAT_SECTIONS = ("identity", "secrets", "templates", "roster")


def state_path(comp_dir):
    return Path(comp_dir) / STATE_NAME


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _resource_identity(entry):
    return (entry["kind"], entry.get("vmid") if entry.get("vmid") is not None
            else (entry.get("bridge") or entry.get("name")))


def _resource_key(entry):
    # Node-scoped: the same bridge name exists on more than one host in a multi-node range.
    return (_resource_identity(entry), entry.get("node") or "")


def _skeleton(comp_dir):
    from common.utils import competition_name
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": {"comp_name": competition_name(comp_dir)},
        "secrets": {},
        "roster": {},
        "placement": None,
        "red": {},
        "templates": {},
        "resources": [],
        "run": {},
        "teardown": {},
        "updated_at": "",
    }


@dataclass
class EstateState:
    comp_dir: Path
    data: dict

    @classmethod
    def load(cls, comp_dir):
        comp_dir = Path(comp_dir)
        path = state_path(comp_dir)
        if not path.exists():
            return cls(comp_dir, _skeleton(comp_dir))
        try:
            data = json.loads(path.read_text())
        except ValueError:
            raise SystemExit(f"  ERROR: {path} is corrupt — refusing to start from a "
                             f"reset state (it holds this competition's secrets and VM "
                             f"ownership; repair or move it aside by hand)")
        version = data.get("schema_version") or 0
        if version > SCHEMA_VERSION:
            raise SystemExit(f"  ERROR: {path} is schema v{version}; this code "
                             f"understands v{SCHEMA_VERSION}. Upgrade tezcatlipoca, or "
                             f"remove the range with the version that wrote it.")
        return cls(comp_dir, data)

    def save(self):
        self.data["updated_at"] = _now()
        write_text_atomic(state_path(self.comp_dir), json.dumps(self.data, indent=2), mode=0o600)

    def section(self, name):
        return self.data.setdefault(name, [] if name in _LIST_SECTIONS else {})

    def record(self, kind, node, name, vmid=None, bridge=None, tags=None):
        entry = {"kind": kind, "node": node, "name": name, "vmid": vmid, "bridge": bridge,
                 "tags": sorted(tags or []), "at": _now()}
        key = _resource_key(entry)
        entries = self.section("resources")
        for existing in entries:
            if _resource_key(existing) == key:
                existing.update(entry)
                return
        entries.append(entry)

    def forget(self, kind, name=None, vmid=None, bridge=None, node=None):
        """Drop one inventory entry; `node=None` means "on any node"."""
        wanted = _resource_identity({"kind": kind, "vmid": vmid, "bridge": bridge,
                                     "name": name})
        entries = self.section("resources")

        def matches(entry):
            return (_resource_identity(entry) == wanted
                    and (node is None or (entry.get("node") or "") == node))

        kept = [e for e in entries if not matches(e)]
        removed = len(entries) - len(kept)
        entries[:] = kept
        return removed

    def resources(self, kind=None, node=None):
        return [e for e in self.section("resources")
                if (kind is None or e["kind"] == kind)
                and (node is None or e["node"] == node)]

    def checkpoint(self, phase):
        self.section("run")["last_phase"] = phase
        self.save()

    def absorb(self, flat, replace=True):
        """Write the flat deploy dict into the document.

        `replace` (the default) makes the document's flat view exactly `flat`: a key absent
        from it is deleted. Every caller passes a full flattened dict, so this is the only
        honest reading of `write_flat`.
        """
        flat = dict(flat)
        for section in _FLAT_SECTIONS:
            target = self.section(section)
            for key, owner in _FLAT_SECTION.items():
                if owner != section:
                    continue
                if key in flat:
                    target[key] = flat.pop(key)
                elif replace and key not in _NEVER_DELETE:
                    target.pop(key, None)
        for key in _MERGED_FLAT:
            if key in flat:
                self.section(key).update(flat.pop(key))
        run = self.section("run")
        if replace:
            run.clear()
        run.update(flat)

    def flatten(self):
        flat = {}
        for key, section in _FLAT_SECTION.items():
            container = self.data.get(section) or {}
            if key in container:
                flat[key] = container[key]
        for key in _MERGED_FLAT:
            value = self.data.get(key) or {}
            if value:
                flat[key] = dict(value)
        flat.update(self.data.get("run") or {})
        return flat


def clear_teardown_journal(comp_dir):
    """Forget a previous range's teardown progress.

    A redeploy reuses the competition's run id, so the journal's own run-id check cannot
    tell the new range from the retired one — and its "done" entries would skip this
    range's evidence collection and red verdict.
    """
    document = EstateState.load(comp_dir)
    if document.data.get("teardown"):
        document.data["teardown"] = {}
        document.save()


def load_all(comps_root):
    return [EstateState.load(path.parent)
            for path in sorted(Path(comps_root).glob(f"*/{STATE_NAME}"))]


def read_flat(comp_dir):
    """The flat state view, or None when no state exists; a document without an identity still reads as state, since `run_id` is what every destroy path requires."""
    comp_dir = Path(comp_dir)
    if not state_path(comp_dir).exists():
        return None
    return EstateState.load(comp_dir).flatten()


def load_flat(comp_dir):
    return read_flat(comp_dir) or {}


def team_vmids(comp_dir):
    return sorted(row["vmid"] for row in EstateState.load(comp_dir).resources("team_box")
                  if row["vmid"])


def write_flat(comp_dir, flat):
    document = EstateState.load(comp_dir)
    document.absorb(flat)
    document.save()
