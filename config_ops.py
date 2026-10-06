"""Competition configuration: packet passwords, team identity/secrets, .env + state writers.

Back-compat facade. The other responsibilities that used to live here are split out and every
name stays importable from `config_ops`:

  preflight/            ALL pre-Proxmox gates (preflight_gates, preflight_gates_multinode,
                        check_datastore_headroom, cloudinit/mgmt-IP/catalog/concurrency gates)
  config_prompts.py     interactive prompts (collect_boxes, collect_users_config, confirm_deploy)
  injects_config.py     load_injects / resolve_inject_times / injects_fingerprint
  pve_api.py            destroy_bridge_if_exists (with the rest of the API client)

Patching a gate/prompt name here no longer changes the moved implementation; patch the owning
module (docs/internals.md)."""

import contextlib
import json
import os
import re
import secrets
import string
from pathlib import Path

from config_prompts import (_prompt_difficulty, collect_boxes, collect_users_config,  # noqa: F401
                            confirm_deploy, list_proxmox_templates)
from constants import (ENGINE_TEMPLATE_VMID_OFFSET, GOLDEN_VMID_OFFSET,
                       MAX_BOXES_PER_TEAM, SCORING_ENGINE_VMID)
from injects_config import injects_fingerprint, load_injects, resolve_inject_times  # noqa: F401
from preflight import (check_datastore_headroom, preflight_gates,  # noqa: F401
                       preflight_gates_multinode, template_cloudinit_missing)
from preflight.catalog import catalog_check_paths as _catalog_check_paths  # noqa: F401
from preflight.catalog import catalog_gate as _catalog_gate  # noqa: F401
from preflight.concurrency import gate_concurrent_deploys as _gate_concurrent_deploys  # noqa: F401
from preflight.mgmt_ip import engine_mgmt_ip_gate as _engine_mgmt_ip_gate  # noqa: F401
from preflight.templates import cloudinit_gate as _cloudinit_gate  # noqa: F401
from pve_api import destroy_bridge_if_exists  # noqa: F401

ENV_PATH = Path(".env")


def load_packet_passwords(comp_dir):
    """Packet-published credentials from passwords.json (compile-packet.py output), or None.

    Present means the deploy uses the packet's default credentials verbatim (that IS the
    competition: teams get them in the packet and rotate at minute zero) instead of
    minting random ones. 0600 + gitignored — future profiles may carry private secrets."""
    path = Path(comp_dir) / "passwords.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise SystemExit(f"  ERROR: {path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"  ERROR: {path} must be a JSON object")
    return data


def load_previous_competitions():
   return [
      p.name
      for p in Path("competitions").iterdir()
      if p.is_dir() and (p / "Compfile").exists()
   ]


def random_password():
    """14 chars, guaranteed upper+lower+digit+symbol, cmd/PS- and URL-safe charset.

    Windows guest boxes set this via `net user` and AD enforces complexity:
    the old letters+digits pool produced digit-free passwords ~13% of runs
    (scrim-extreme-2026-09-20) and the policy rejection killed every Windows
    login downstream. Characters are cmd/PS-quoting safe (no &|<>^%$`"' or
    whitespace) and URL-grammar free (no #/?@): these secrets land in
    postgres DSNs in /opt/quotient/.env, and `#` silently truncates the DSN
    at the password (scrim-extreme-cyberfield-2026-09-22 crash loop).

    Single source of truth for every generated secret, including packet_ops' decoy
    baseline accounts (which used to carry a copy of this generator, audit 2026-10-02).
    Changing the charset or length changes behaviour everywhere at once — deliberately."""
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!*_-+="]
    alphabet = "".join(pools)
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if all(any(c in p for c in pw) for p in pools):
            return pw


def collect_teams(number_of_teams, engine_vmid=SCORING_ENGINE_VMID):
    teams = {}
    import os as _os
    override = (_os.environ.get("TF_VAR_team_identifiers") or "").strip()
    ids = [s.strip() for s in override.split(",")] if override else [
        str(100 + i) for i in range(1, number_of_teams + 1)
    ]
    if override and len(ids) < number_of_teams:
        ids += [str(100 + i) for i in range(len(ids) + 1, number_of_teams + 1)]
    seen = set()
    for i in range(1, number_of_teams + 1):
        key = f"team{i}"
        identifier = ids[i - 1]
        if not (identifier.isdigit() and 1 <= int(identifier) <= 254):
            raise SystemExit(
                f"  ERROR: team identifier {identifier!r} must be an integer in 1..254 "
                f"(it becomes the 192.168.<id>.x subnet)."
            )
        if identifier in seen:
            raise SystemExit(f"  ERROR: duplicate team identifier {identifier} — subnets/vmids would collide.")
        seen.add(identifier)
        base = 200 + int(identifier) * 10
        # A team's vmid block must clear not just the engine but its DERIVED slots:
        # engine template (engine+140) and the golden block (engine+150..+159). The
        # old check covered the engine alone (live-found 2026-09-29: engine 1080 +
        # default identifiers put team2's first box vmid exactly on the engine
        # template slot at 1220 — the collision only surfaces at terraform apply #2,
        # hours into the deploy). Phase 1's wave logic treats all 10 slots per block,
        # so the guard is full-width.
        reserved = ({engine_vmid}
                    | set(range(engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET,
                                engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET + MAX_BOXES_PER_TEAM))
                    | set(range(engine_vmid + GOLDEN_VMID_OFFSET,
                                engine_vmid + GOLDEN_VMID_OFFSET + MAX_BOXES_PER_TEAM)))
        block = set(range(base, base + MAX_BOXES_PER_TEAM))
        if block & reserved:
            raise SystemExit(
                f"  ERROR: team identifier {identifier} maps to vmids "
                f"{base}..{base + MAX_BOXES_PER_TEAM - 1}, overlapping this engine's "
                f"reserved slots (engine {engine_vmid}, engine template "
                f"{engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET}, goldens "
                f"{engine_vmid + GOLDEN_VMID_OFFSET}+) — terraform apply #2 would "
                f"clone a team box onto one of them. Pick different "
                f"TF_VAR_team_identifiers or --scoring-vmid.")
        password = random_password()
        teams[key] = {"identifier": identifier, "password": password}
    return teams


def update_env(updates: dict):
    text = ENV_PATH.read_text()
    for key, value in updates.items():
        line = f"{key}={value}"
        new_text, count = re.subn(rf"^{re.escape(key)}=.*$", lambda _m: line, text, flags=re.MULTILINE)
        text = new_text if count else text + f"\n{line}\n"
        os.environ[key] = value
    write_text_atomic(ENV_PATH, text, mode=0o600)


def write_text_atomic(path, text, mode=0o600):
    """Write `text` to `path` so a torn write can never be observed, then chmod.

    Both halves matter and both have bitten this repo:

    *Atomicity* — `.deploy_state.json` holds the only copy of the generated box and
    team passwords; a partial write bricks resume AND redeploy at once (deploy.py
    documented this after the fact, but three other writers of the same file used a
    plain write_text, so the guarantee held at exactly one of four call sites).

    *Mode at creation* — creating with the process umask and chmod-ing afterwards
    leaves a window where the file is world-readable. os.open() applies the mode
    atomically with creation, so the secret is never briefly public.
    """
    path = Path(path)
    tmp_path = path.with_name(path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    os.replace(tmp_path, path)


def write_state(path, state):
    """Persist a JSON state dict atomically, 0600. Single writer for .deploy_state.json."""
    write_text_atomic(path, json.dumps(state, indent=2), mode=0o600)


def load_boxes(comp_dir):
    path = comp_dir / "boxes.json"
    return json.loads(path.read_text()) if path.exists() else None

__all__ = [
    "ENV_PATH",
    "_catalog_check_paths",
    "_catalog_gate",
    "_cloudinit_gate",
    "_engine_mgmt_ip_gate",
    "_gate_concurrent_deploys",
    "_prompt_difficulty",
    "check_datastore_headroom",
    "collect_boxes",
    "collect_teams",
    "collect_users_config",
    "confirm_deploy",
    "destroy_bridge_if_exists",
    "injects_fingerprint",
    "list_proxmox_templates",
    "load_boxes",
    "load_injects",
    "load_packet_passwords",
    "load_previous_competitions",
    "preflight_gates",
    "preflight_gates_multinode",
    "random_password",
    "resolve_inject_times",
    "template_cloudinit_missing",
    "update_env",
    "write_state",
    "write_text_atomic",
]
