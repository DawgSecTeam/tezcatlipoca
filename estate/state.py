"""The competition's single estate state document (schema v1)."""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from config.config_ops import write_text_atomic

SCHEMA_VERSION = 1
STATE_NAME = "estate-state.json"

_LIST_SECTIONS = {"resources"}

# The deploy's flat state keys live in these sections. Everything not named in
# _FLAT_SECTION lands in `run`, the deploy-bookkeeping catch-all. The flat dict is the
# in-memory shape the eight phases already use, so this map is the one place the two
# shapes are reconciled; `absorb` never deletes, so structured keys survive a round trip.
_SECTIONS = ("identity", "secrets", "roster", "templates", "run")
_FLAT_SECTION = {
    "comp_name": "identity", "run_id": "identity", "scoring_vm_id": "identity",
    "engine_mgmt_ip": "identity", "engine_mgmt_gw": "identity", "endpoint": "identity",
    "node": "identity", "datastore": "identity",
    "teams": "secrets", "admin_password": "secrets", "scoring_password": "secrets",
    "postgres_password": "secrets", "redis_password": "secrets", "box_password": "secrets",
    "box_creds": "secrets", "domain_creds": "secrets", "inject_password": "secrets",
    "engine_build_info": "templates", "engine_template_vmid": "templates",
    "engine_template_hash": "templates", "golden_template_ids": "templates",
    "golden_ids_by_slot": "templates", "golden_hashes": "templates",
    "boxes": "roster",
}


def state_path(comp_dir):
    """Path of the competition's estate state file."""
    return Path(comp_dir) / STATE_NAME


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _resource_key(entry):
# A vmid identifies a VM, a bridge a bridge, a name everything else (templates, jumps).
    if entry.get("vmid") is not None:
        return (entry["kind"], entry["vmid"])
    if entry.get("bridge") is not None:
        return (entry["kind"], entry["bridge"])
    return (entry["kind"], entry["name"])


def _skeleton(comp_dir):
    from common.utils import competition_name
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": {"comp_name": competition_name(comp_dir)},
        "secrets": {},
        "roster": {},
        "placement": None,
        "templates": {},
        "resources": [],
        "run": {},
        "external": {},
        "teardown": {},
        "updated_at": "",
    }


@dataclass
class EstateState:
    """One competition's state document and the directory it lives in."""

    comp_dir: Path
    data: dict

    @classmethod
    def load(cls, comp_dir):
        """The competition's state, or a fresh skeleton when none is written yet."""
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
        """Persist the state file atomically, 0600."""
        self.data["updated_at"] = _now()
        write_text_atomic(state_path(self.comp_dir), json.dumps(self.data, indent=2))

    def section(self, name):
        """The top-level section, created empty (a list for resources)."""
        return self.data.setdefault(name, [] if name in _LIST_SECTIONS else {})

    def record(self, kind, node, name, vmid=None, bridge=None, tags=None):
        """Upsert one resource entry, keyed by vmid, else bridge, else name."""
        key = _resource_key({"kind": kind, "vmid": vmid, "bridge": bridge, "name": name})
        entry = {"kind": kind, "node": node, "name": name, "vmid": vmid, "bridge": bridge,
                 "tags": list(tags or []), "at": _now()}
        entries = self.section("resources")
        for existing in entries:
            if _resource_key(existing) == key:
                existing.update(entry)
                return
        entries.append(entry)

    def forget(self, kind, name=None, vmid=None, bridge=None):
        """Drop the matching resource entries; returns how many were removed."""
        key = _resource_key({"kind": kind, "vmid": vmid, "bridge": bridge, "name": name})
        entries = self.section("resources")
        before = len(entries)
        entries[:] = [e for e in entries if _resource_key(e) != key]
        return before - len(entries)

    def resources(self, kind=None, node=None):
        """The resource entries matching kind and/or node."""
        return [e for e in self.section("resources")
                if (kind is None or e["kind"] == kind)
                and (node is None or e["node"] == node)]

    def checkpoint(self, phase):
        """Record a completed phase and persist."""
        self.section("run")["last_phase"] = phase
        self.save()

    def absorb(self, flat):
        """Write the deploy's flat state dict into the document's sections."""
        for key, value in flat.items():
            self.section(_FLAT_SECTION.get(key, "run"))[key] = value

    def flatten(self):
        """The deploy's flat state dict, rebuilt from the document's sections."""
        flat = {}
        for section in _SECTIONS:
            flat.update(self.section(section))
        return flat


def load_all(comps_root):
    """Every competition state under a comps root."""
    return [EstateState.load(path.parent)
            for path in sorted(Path(comps_root).glob(f"*/{STATE_NAME}"))]


def read_flat(comp_dir):
    """The competition's flat state view, or None when it has no state at all.

    A document that exists WITHOUT an identity reads as state: `run_id` is the ownership
    key every destroy path requires, and a caller that finds none must refuse rather than
    fall back to a second source.
    """
    comp_dir = Path(comp_dir)
    if not state_path(comp_dir).exists():
        return None
    return EstateState.load(comp_dir).flatten()


def load_flat(comp_dir):
    """The competition's flat state view; {} when it has no state yet."""
    return read_flat(comp_dir) or {}


def team_vmids(comp_dir):
    """Every recorded team-box vmid, sorted — the resume gate's "is the work there" probe."""
    return sorted(row["vmid"] for row in EstateState.load(comp_dir).resources("team_box")
                  if row["vmid"])


def write_flat(comp_dir, flat):
    """Persist a flat state dict into the competition's document, atomically."""
    document = EstateState.load(comp_dir)
    document.absorb(flat)
    document.save()
