"""Competition configuration, team/box prompts, and inject handling."""

import json
import os
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path

import requests

from constants import (ENGINE_TEMPLATE_VMID_OFFSET, MAX_BOXES_PER_TEAM, NAKON_DIR,
                       SCORING_ENGINE_VMID)
from range_ops import has_clone_marker, proxmox_api, proxmox_request, vm_id_for
from utils import (BOX_USERNAME_DEFAULT, CREDLIST_USERNAMES_DEFAULT, is_legacy_account_name,
                   valid_unix_username)

ENV_PATH = Path(".env")


def preflight_gates(comp_dir, boxes, num_teams, teams=None,
                    engine_vmid=SCORING_ENGINE_VMID, check_free=True):
    """Blocking pre-apply gates: template resolution, vmid/bridge collisions,
    datastore headroom, catalog check.

    Each previously surfaced as a mid-deploy corpse (docs/e2e-testing.md §6): a
    missing template died inside terraform apply, a full datastore died mid-clone,
    a bad pin died mid-plant. The collision gate (check_free) makes concurrent
    competitions on one node safe: it fails fast if this comp's engine vmid, any
    team vmid, or any team bridge already exists (i.e. belongs to another range)."""
    node = os.environ["TF_VAR_proxmox_node"]
    try:
        vms = proxmox_api("GET", "/cluster/resources", params={"type": "vm"})["data"]
    except Exception as e:
        raise SystemExit(f"  ERROR: Proxmox API unreachable during preflight: {e}")
    tagged = {vm.get("name") for vm in vms
              if vm.get("template") == 1 and "template" in (vm.get("tags") or "").split(";")}
    missing = sorted({b["template"] for b in boxes} - tagged)
    if missing:
        raise SystemExit(
            "  ERROR: box template(s) with no tagged template on the cluster: "
            + ", ".join(missing)
            + ". Clones would fail mid-apply; available: "
            + (", ".join(sorted(t for t in tagged if t)) or "(none)"))
    engine_base = int(os.environ["TF_VAR_template_vm_id"])
    if not any(vm.get("vmid") == engine_base for vm in vms):
        raise SystemExit(
            f"  ERROR: engine base image vmid {engine_base} (TF_VAR_template_vm_id) does "
            f"not exist on this cluster — the engine-template build and the scoring "
            f"engine clone would fail mid-apply.")
    print(f"  Preflight: all {len(boxes)} box template(s) resolve; engine base image vmid "
          f"{engine_base} present")

    if check_free and teams:
        existing_vmids = {vm.get("vmid") for vm in vms}
        vm_by_vmid = {vm.get("vmid"): vm for vm in vms}
        # A retry of THIS competition's failed deploy meets its own leftovers. A VM tagged
        # tezcatlipoca + comp-<name> is ours by creation (main.tf / clone_ops / golden_ops
        # all tag); phase 1's ownership-checked cleanup destroys it. Anything else on our
        # vmids is genuinely foreign and stays fatal.
        our_tags = {"tezcatlipoca", f"comp-{comp_dir.name}"}

        def _is_ours(vmid, expected_name=None):
            vm = vm_by_vmid.get(vmid)
            if vm is None:
                return False
            vtags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
            if our_tags <= vtags:
                return True
            # Interrupted clone: untagged (the tag PUT never ran) but carrying the clone
            # marker written in the clone POST itself — ours; phase 1 unlocks + destroys it.
            if not vtags and has_clone_marker(node, vmid, comp_dir.name):
                print(f"  Preflight: vmid {vmid} is an interrupted clone of this competition "
                      f"(lock={vm.get('lock') or 'none'}) — phase 1 will clean it")
                return True
            # Legacy M4 golden slots predate ownership tags. Adopt only the exact
            # reserved golden VM/name pair; all other untagged resources remain foreign.
            return (expected_name is not None and vm.get("name") == expected_name
                    and vtags == {"template"})

        clashes = []
        foreign = 0
        ours = 0

        def _clash(label, vmid, expected_name=None):
            nonlocal foreign, ours
            if _is_ours(vmid, expected_name=expected_name):
                ours += 1
            else:
                foreign += 1
                clashes.append(label)

        if engine_vmid in existing_vmids:
            _clash(f"scoring engine vmid {engine_vmid}", engine_vmid)
        # M4: the engine template's reserved slot (just below the golden block). A VM
        # there tagged as ours is the competition's persistent template — expected to
        # survive phase 1 and be reused, NOT a leftover to clean. Foreign = fatal.
        et_vmid = engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET
        if et_vmid in existing_vmids:
            if _is_ours(et_vmid):
                print(f"  Preflight: engine template vmid {et_vmid} present (M4 persistent — reused)")
            else:
                foreign += 1
                clashes.append(f"engine template vmid {et_vmid}")
        for team_key, team in teams.items():
            for box_idx in range(len(boxes)):
                vid = vm_id_for(team["identifier"], box_idx)
                if vid in existing_vmids:
                    _clash(f"team vmid {vid} ({team_key}/{boxes[box_idx]['name']})", vid)
        for box_idx in range(len(boxes)):
            vid = engine_vmid + 150 + box_idx
            if vid in existing_vmids:
                _clash(f"golden vmid {vid} ({boxes[box_idx]['name']})", vid,
                       expected_name=f"golden-{boxes[box_idx]['name']}")
        try:
            nets = proxmox_api("GET", f"/nodes/{node}/network")["data"]
            existing_bridges = {n.get("iface") for n in nets}
        except Exception:
            existing_bridges = set()
        for team in teams.values():
            bridge = f"vmbr{team['identifier']}"
            # Bridges carry no per-comp tags. Tolerate one only when the VM leftovers are
            # unambiguously all ours (a retry) — a foreign bridge stays fatal.
            if bridge in existing_bridges:
                if foreign == 0 and ours > 0:
                    ours += 1
                else:
                    clashes.append(f"bridge {bridge}")
        if clashes:
            raise SystemExit(
                "  ERROR: this competition's infrastructure collides with VMs/bridges already "
                "on node '" + node + "' (another running competition?): " + ", ".join(clashes)
                + ". Pick a free --scoring-vmid and/or non-overlapping TF_VAR_team_identifiers. "
                "Note the golden block sits at <scoring-vmid>+150 — a colliding golden vmid "
                "also means picking a different engine vmid.")
        if ours:
            print(f"  Preflight: {ours} leftover VM(s)/bridge(s) tagged as this "
                  f"competition's — phase 1 cleans or (M4 hash-matching templates) reuses them")
        else:
            print(f"  Preflight: engine vmid {engine_vmid}, engine template vmid {et_vmid}, "
                  f"golden vmids {engine_vmid + 150}+, all team vmids, and team bridges are free")

    datastore = os.environ.get("TF_VAR_datastore", "local-lvm")
    # The storage LIST zeroes/omits free on some pools (cyberfield hdrives-zfs);
    # the per-store STATUS endpoint's avail is the authoritative number.
    try:
        st = proxmox_api("GET", f"/nodes/{node}/storage/{datastore}/status")["data"]
        free = st.get("avail")
    except Exception:
        free = None
    if free is None:
        print(f"  WARNING: could not read free space on datastore '{datastore}' — "
              f"headroom unchecked")
    else:
        free_gb = free / 1024 ** 3
        # disk_gb unset means "template's own disk", unknowable here; 40 GB is a
        # conservative stand-in across the base templates.
        need_gb = num_teams * sum(b.get("disk_gb") or 40 for b in boxes)
        if free_gb < need_gb:
            raise SystemExit(
                f"  ERROR: datastore '{datastore}' has {free_gb:.0f} GB free; this deploy "
                f"needs ~{need_gb:.0f} GB ({num_teams} teams x {len(boxes)} boxes, unset disk "
                f"sizes counted as 40 GB). Free space or trim the competition first.")
        print(f"  Preflight: datastore '{datastore}' {free_gb:.0f} GB free vs "
              f"~{need_gb:.0f} GB needed")

    catalog = subprocess.run(
        [sys.executable, "-m", "nakon", "catalog", "check",
         "--boxes-json", str((comp_dir / "boxes.json").resolve()),
         "--box-services", str((comp_dir / "box_services.json").resolve()),
         "--box-vulns", str((comp_dir / "box_vulns.json").resolve())],
        cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=600)
    if catalog.returncode != 0:
        print(catalog.stdout)
        print(catalog.stderr)
        raise SystemExit(
            "  ERROR: nakon catalog check reported errors for this competition's pins — "
            "fix or trim box_vulns.json/box_services.json before deploying (details above).")
    print("  Preflight: nakon catalog check 0 errors")


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
    at the password (scrim-extreme-cyberfield-2026-09-22 crash loop)."""
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
        if base <= engine_vmid <= base + MAX_BOXES_PER_TEAM - 1:
            raise SystemExit(
                f"  ERROR: team identifier {identifier} maps to vmids "
                f"{base}..{base + MAX_BOXES_PER_TEAM - 1}, colliding with the scoring engine "
                f"(vmid {engine_vmid})."
            )
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
    ENV_PATH.write_text(text)


def list_proxmox_templates():
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    scoring_template_id = int(os.environ["TF_VAR_template_vm_id"])
    try:
        r = proxmox_request(
            "GET", f"{endpoint}/api2/json/cluster/resources",
            params={"type": "vm"},
            headers={"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"},
            timeout=10,
        )
        r.raise_for_status()
        vms = r.json()["data"]
    except Exception as e:
        print(f"  (couldn't list Proxmox templates: {e})")
        return []
    return sorted(
        vm["name"] for vm in vms
        if vm.get("template") == 1
        and "template" in (vm.get("tags") or "").split(";")
        and vm.get("vmid") != scoring_template_id
    )


def destroy_bridge_if_exists(node, bridge_name):
    """Delete a Proxmox Linux bridge if it exists. A GET-first existence check keeps
    a fresh deploy (whose bridges are all new) from spraying spurious 400s — PVE
    rejects DELETE with "Parameter verification failed" rather than 404 for an
    absent iface (live noise, m4-validation-2026-09-25 phase 1)."""
    try:
        existing = {n.get("iface") for n in proxmox_api("GET", f"/nodes/{node}/network")["data"]}
        if bridge_name not in existing:
            return
        proxmox_api("DELETE", f"/nodes/{node}/network/{bridge_name}")
    except requests.exceptions.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            print(f"    WARNING: could not delete bridge {bridge_name}: {e}")
    except Exception as e:
        print(f"    WARNING: could not delete bridge {bridge_name}: {e}")


def _prompt_int(prompt, default):
    """int(input()) with a default on blank and a re-prompt (not a crash) on non-numeric input."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"  Enter a whole number (or leave blank for {default}).")


def _prompt_difficulty():
    """Difficulty prompt that re-asks on non-numeric/out-of-range input instead of crashing."""
    while True:
        raw = input("Difficulty (1-10): ").strip()
        try:
            value = int(raw)
        except ValueError:
            print("  Enter a whole number from 1 to 10.")
            continue
        if 1 <= value <= 10:
            return value
        print("  Enter a number from 1 to 10.")


def _prompt_optional_int(prompt):
    """Like _prompt_int, but blank means None (no default) instead of a fallback value."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            print("  Enter a whole number, or leave blank.")


def collect_boxes():
    templates = list_proxmox_templates()
    if templates:
        print("Available box templates (tagged 'template' in Proxmox):")
        for i, t in enumerate(templates, 1):
            print(f"  [{i}] {t}")
        print()
    else:
        print("  (no templates found in Proxmox — you'll need to type template names manually)\n")

    while True:
        raw = input("How many box types for this competition? ").strip()
        try:
            number_of_boxes = int(raw)
        except ValueError:
            print("  Enter a whole number.")
            continue
        if 1 <= number_of_boxes <= MAX_BOXES_PER_TEAM:
            break
        print(f"  Enter a number from 1 to {MAX_BOXES_PER_TEAM} (see MAX_BOXES_PER_TEAM).")

    boxes = []
    for i in range(1, number_of_boxes + 1):
        print(f"\n─── Box {i} of {number_of_boxes} " + "─" * 40)

        existing_names = {b["name"] for b in boxes}
        name = input("  Name (e.g. web01, db, mail): ").strip()
        while not name or name in existing_names:
            if name in existing_names:
                name = input(f"  '{name}' is already used by another box in this competition"
                              " — pick a different name: ").strip()
            else:
                name = input("  Name can't be blank: ").strip()

        if templates:
            while True:
                raw = input(f"  Template [{1}–{len(templates)}, or name]: ").strip()
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(templates):
                        template = templates[idx - 1]
                        break
                except ValueError:
                    pass
                if raw in templates:
                    template = raw
                    break
                print(f"  Enter a number from 1 to {len(templates)}, or a template name.")
        else:
            template = input("  Template name: ").strip()
            while not template:
                template = input("  Template can't be blank — must match a tagged Proxmox VM exactly: ").strip()

        cpu = _prompt_int("  CPU cores    [1]: ", 1)
        memory_mb = _prompt_int("  Memory (MB)  [2048]: ", 2048)
        disk_gb = _prompt_optional_int("  Disk (GB)    [keep template's]: ")

        box = {
            "name": name, "last_octet": i + 1, "cpu": cpu, "memory_mb": memory_mb,
            "disk_gb": disk_gb, "template": template,
        }
        boxes.append(box)
    return boxes


def collect_users_config(box_username_flag=None, credlist_flag=None):
    """Collect themeable box login + 3 credlist usernames; defaults when blank, always writes users.json."""

    if box_username_flag is not None:
        box_username = box_username_flag.strip() or BOX_USERNAME_DEFAULT
    else:
        box_username = input(f"  Box login username [{BOX_USERNAME_DEFAULT}]: ").strip() or BOX_USERNAME_DEFAULT
    if not valid_unix_username(box_username):
        print(f"  '{box_username}' is not a valid box username — using {BOX_USERNAME_DEFAULT}.")
        box_username = BOX_USERNAME_DEFAULT
    elif is_legacy_account_name(box_username):
        print(f"  '{box_username}' collides with a legacy distro system account (cloud-init "
              f"would adopt it and brick auth) — using {BOX_USERNAME_DEFAULT} instead.")
        box_username = BOX_USERNAME_DEFAULT

    if credlist_flag is not None:
        raw = credlist_flag
    else:
        raw = input(
            f"  Credlist usernames, comma-separated (exactly 3) "
            f"[{','.join(CREDLIST_USERNAMES_DEFAULT)}]: "
        ).strip()

    if raw:
        credlist_usernames = [n.strip() for n in raw.split(",") if n.strip()]
        if len(credlist_usernames) != 3:
            print(f"  Need exactly 3 credlist usernames — got {len(credlist_usernames)}, "
                  f"falling back to the default {CREDLIST_USERNAMES_DEFAULT}.")
            credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
        elif not all(valid_unix_username(n) for n in credlist_usernames):
            print(f"  Credlist usernames must be lowercase [a-z_][a-z0-9_-]* — "
                  f"falling back to the default {CREDLIST_USERNAMES_DEFAULT}.")
            credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)
    else:
        credlist_usernames = list(CREDLIST_USERNAMES_DEFAULT)

    return box_username, credlist_usernames


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


def load_boxes(comp_dir):
    path = comp_dir / "boxes.json"
    return json.loads(path.read_text()) if path.exists() else None


def confirm_deploy(name, scenario, difficulty, teams, boxes):
    n = len(teams)
    team_range = f"team1–team{n}" if n > 1 else "team1"
    short_scenario = scenario[:72] + ("..." if len(scenario) > 72 else "")

    print("\n─── Ready to deploy " + "─" * 44)
    print(f"  Competition : {name}")
    print(f"  Scenario    : {short_scenario}")
    print(f"  Difficulty  : {difficulty} / 10")
    print(f"  Teams       : {n}  ({team_range}, passwords auto-generated)")
    print(f"  Boxes       :")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}  ({b['cpu']} CPU, {b['memory_mb']} MB)")
    print()
    print("  Terraform will now run; this takes several minutes.")
    answer = input("  Continue? (y/n): ").strip().lower()
    return answer == "y"
