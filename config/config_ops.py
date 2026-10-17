"""Competition configuration: packet passwords, team identity/secrets, state writers."""

import contextlib
import json
import os
import secrets
import string
from pathlib import Path

from common.constants import (ENGINE_TEMPLATE_VMID_OFFSET, GOLDEN_VMID_OFFSET,
                       MAX_BOXES_PER_TEAM, SCORING_ENGINE_VMID)
from common.comp_paths import comps_root
from estate import config


def load_packet_passwords(comp_dir):
# Present when the deploy uses the packet's published credentials verbatim rather than
# minting random ones (that IS the competition: teams rotate at minute zero).
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
      for p in comps_root().iterdir()
      if p.is_dir() and (p / "Compfile").exists()
   ]


def random_password():
# 14 chars with a guaranteed upper+lower+digit+symbol, cmd/PS-quoting safe and URL-grammar
# free (`#` silently truncates a postgres DSN; AD complexity rejects a digit-free password).
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, "!*_-+="]
    alphabet = "".join(pools)
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(14))
        if all(any(c in p for c in pw) for p in pools):
            return pw


def collect_teams(number_of_teams, engine_vmid=SCORING_ENGINE_VMID,
                  identifier_defaults=None, team_identifiers=None):
    teams = {}
    # An explicit per-run list outranks the ambient config: two ranges of one competition on a
    # shared node need different blocks.
    override = (team_identifiers or config.active().knobs.team_identifiers or "").strip()
    if override:
        ids = [s.strip() for s in override.split(",")]
        if len(ids) < number_of_teams:
            ids += [str(100 + i) for i in range(len(ids) + 1, number_of_teams + 1)]
    else:
        ids = [str(value) for value in (identifier_defaults or [])[:number_of_teams]]
        candidate = 101
        while len(ids) < number_of_teams:
            value = str(candidate)
            if value not in ids:
                ids.append(value)
            candidate += 1
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
        # A team's vmid block must clear the engine's DERIVED slots too (engine template at
        # +140, goldens at +150..+159): checking the engine alone put team2's first box vmid
        # exactly on the engine template slot. Phase 1 treats all 10 slots per block.
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
                f"{engine_vmid + GOLDEN_VMID_OFFSET}+) — the team-box clone pass "
                f"would put one of them on that slot. Pick different "
                f"team identifiers or --scoring-vmid.")
        password = random_password()
        teams[key] = {"identifier": identifier, "password": password}
    return teams



def write_text_atomic(path, text, mode=0o600):
# Atomicity matters (the state document holds the only copy of the generated passwords) and
# the mode must apply at creation: creating then chmod-ing leaves the secret briefly readable.
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
    write_text_atomic(path, json.dumps(state, indent=2), mode=0o600)


def load_boxes(comp_dir):
    path = comp_dir / "boxes.json"
    return json.loads(path.read_text()) if path.exists() else None

__all__ = [
    "collect_teams",
    "load_boxes",
    "load_packet_passwords",
    "load_previous_competitions",
    "random_password",
    "write_state",
    "write_text_atomic",
]
