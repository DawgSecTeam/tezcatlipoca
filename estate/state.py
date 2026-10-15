"""The competition's single estate state document (schema v1)."""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from config.config_ops import write_text_atomic

SCHEMA_VERSION = 1
STATE_NAME = "estate-state.json"
# The estate pipeline's own version; common.constants.PIPELINE_VERSION tracks the
# terraform-era deploy pipeline this component replaces.
PIPELINE_VERSION = 4

_LIST_SECTIONS = {"resources"}


def state_path(comp_dir):
    """Path of the competition's estate state file."""
    return Path(comp_dir) / STATE_NAME


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def _resource_key(entry):
# A vmid identifies a VM, a bridge a bridge, a name everything else (templates, jumps).
    if entry.get("vmid") is not None:
        return (entry["kind"], entry["vmid"])
    if entry.get("bridge") is not None:
        return (entry["kind"], entry["bridge"])
    return (entry["kind"], entry["name"])


def _skeleton(comp_dir):
    return {
        "schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "identity": {"comp_name": comp_dir.name},
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

    @classmethod
    def migrate(cls, comp_dir):
        """Build and persist this competition's state from its legacy files."""
        comp_dir = Path(comp_dir)
        legacy = _read_json(comp_dir / ".deploy_state.json") or {}
        hashes = _read_json(comp_dir / ".template-hashes.json") or {}
        placement = _read_json(comp_dir / "placement.json")
        targets = (_read_json(comp_dir / "targets.json") or {}).get("targets") or {}
        journal = _read_json(comp_dir / ".teardown-journal.json") or {}
        teams = legacy.get("teams") or _read_json(comp_dir / "teams.json") or {}
        node = os.environ.get("TF_VAR_proxmox_node") or ""
        slots = (placement or {}).get("team_slots") or {}
        golden_vmids = legacy.get("golden_template_ids") or {}
        engine_hashes = hashes.get("engine")

        data = _skeleton(comp_dir)
        data["identity"] = {
            "comp_name": comp_dir.name,
            # The Compfile's display name was never copied into .deploy_state.json.
            "name": "",
            "run_id": legacy.get("run_id") or "",
            "scoring_vm_id": legacy.get("scoring_vm_id"),
            # Neither is in .deploy_state.json; only the env this migration runs with has them.
            "engine_mgmt_ip": os.environ.get("TF_VAR_engine_mgmt_ip") or "",
            "engine_mgmt_gw": os.environ.get("TF_VAR_engine_mgmt_gw") or "",
            "endpoint": os.environ.get("TF_VAR_proxmox_endpoint") or "",
            "node": node,
            "datastore": os.environ.get("TF_VAR_datastore") or "",
        }
        data["secrets"] = {
            **{key: legacy[key] for key in ("admin_password", "scoring_password",
                                            "postgres_password", "redis_password",
                                            "box_password", "box_creds", "domain_creds",
                                            "inject_password") if key in legacy},
            "teams": {key: {"identifier": team.get("identifier"),
                            "password": team.get("password")}
                      for key, team in teams.items()},
        }
        data["roster"] = {
            "boxes": _read_json(comp_dir / "boxes.json") or [],
            # targets.json froze vmid/ip per vm_name but carries no slot.
            "targets": {vm: {"vmid": t.get("vmid"), "ip": t.get("ip"),
                             "node": t.get("node") or node,
                             "slot": slots.get(t.get("team_key")),
                             "box_name": t.get("box_name"), "team_key": t.get("team_key")}
                        for vm, t in targets.items()},
        }
        data["placement"] = placement
        data["templates"] = {
            "engine": {**engine_hashes, "vmid": legacy.get("engine_template_vmid")}
                      if engine_hashes else None,
            "golden": {box: {**entry, "vmid": golden_vmids.get(box)}
                       for box, entry in (hashes.get("golden") or {}).items()},
            "golden_by_slot": legacy.get("golden_ids_by_slot") or {},
            "golden_planted": hashes.get("golden_planted") or {},
            "frozen": _read_json(comp_dir / ".frozen.json"),
        }
        data["run"] = {key: legacy[key] for key in (
            "last_phase", "hold_start", "paused_after_phase", "deployed_endpoint",
            "engine_build_info", "portal_up", "portal_url", "nakon_failed_steps",
            "degradations") if key in legacy}
        # The deploy called this machine -> failed-steps map plant_coverage_failed.
        data["run"]["plant_coverage"] = legacy.get("plant_coverage_failed") or {}
        data["external"] = {
            "headscale": legacy.get("remote_access") or {},
            "portal": {"up": bool(legacy.get("portal_up")),
                       "url": legacy.get("portal_url") or ""},
            "red01": {"vmid": (legacy.get("assume_breach") or {}).get("vmid")},
        }
        data["teardown"] = {"run_id": journal.get("run_id") or "",
                            "done": journal.get("done") or {},
                            "updated_at": journal.get("updated_at") or ""}
        state = cls(comp_dir, data)
        state.save()
        return state

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


def load_all(comps_root):
    """Every competition state under a comps root."""
    return [EstateState.load(path.parent)
            for path in sorted(Path(comps_root).glob(f"*/{STATE_NAME}"))]
