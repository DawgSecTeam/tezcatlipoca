"""Generate nakon machine lists: randomized config, per-slot golden config, staged configs."""

import json
import math
import os
import re
import subprocess
import sys

from common.constants import (
    ACCOUNT_FILE_MODE_CONFIGS,
    DISRUPTIVE_CONFIGS,
    DOMAIN_INFRA_CONFIGS,
    FINAL_STAGE_CONFIGS,
    GOLDEN_IP_BASE,
    NAKON_DIR,
    PIN_CHECK_OVERRIDES,
    POST_CLONE_CONFIGS,
    PREDOMAIN_STAGE_CONFIGS,
    REQUIRED_VARS,
    SLOW_SERVICES,
    is_domain_dependent,
    WINDOWS_ADMIN_USER,
)
from common.utils import is_unmanaged
from common.windows_ops import is_windows_template
from nakonops.nakon_pin_ops import (
    _config_name,
    _pin_key,
    _cross_box_ip,
    _identity_banned_configs,
    _fill_identity_vars,
    _validate_pin_vars,
    _validate_windows_local_user_pins,
    _validate_known_broken_pins,
    _drop_unplantable_bare,
    _drop_catalog_var_bare,
    catalog_required_var_names,
)


_ACCOUNT_FILE_PATH = re.compile(r"^/etc/(passwd|shadow|group|gshadow)$")


def _late_mode_names(machines):
    # Pins whose planted effect is a MODE on an account file the deploy rewrites. Classify by the
    # pin's own FILE_PATH vars too, or a pin asking for /etc/shadow rides an earlier pass and dies.
    names = set()
    for m in machines:
        for c in m["configurations"]:
            name = _config_name(c)
            if name in ACCOUNT_FILE_MODE_CONFIGS or (
                    isinstance(c, dict)
                    and any(isinstance(v, str) and _ACCOUNT_FILE_PATH.match(v)
                            for v in (c.get("vars") or {}).values())):
                names.add(name)
    return names


def os_to_platform(template):
    return "windows" if "win" in template.lower() else "linux"


def _nakon_randomize(platform, services_budget, vulns_budget):
    cmd = [
        sys.executable, "-m", "nakon", "randomize",
        "--platform", platform,
        "--services", str(services_budget),
        "--vulns", str(vulns_budget),
        "--exclude", *SLOW_SERVICES,
        "--source", "auto", "--json",
    ]
    result = subprocess.run(cmd, cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon randomize failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine, or VULNDB_UI_URL set. Check vendor/nakon/.env."
        )
    selection = json.loads(result.stdout.strip().splitlines()[-1])
    return selection["services"], selection["vulns"]


def generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password, box_username="ubuntu"):
    services_path = comp_dir / "box_services.json"
    vulns_path = comp_dir / "box_vulns.json"

    reused = services_path.exists() or vulns_path.exists()
    if reused:
        pinned = json.loads(services_path.read_text()) if services_path.exists() else {}
        pinned_vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
        box_configs = {
            box["name"]: (pinned.get(box["name"], []), pinned_vulns.get(box["name"], []))
            for box in boxes if not is_unmanaged(box)
        }
        pinned_from = ", ".join(
            p.name for p in (services_path, vulns_path) if p.exists()
        )
        print(f"  Using pinned configurations from {pinned_from}")
        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        vulns_path.write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )
    else:
        box_configs = {}
        for box in boxes:
            if is_unmanaged(box):
                continue  # firewall/appliance: no scored services, no planted vulns
            platform = os_to_platform(box["template"])
            services, vulns = _nakon_randomize(
                platform, max(math.ceil(difficulty / 3), 1), max(difficulty, 1)
            )
            # randomize cannot know the driver-side broken list, so drop those names here.
            where = f"fresh selection for '{box['name']}'"
            services = _drop_unplantable_bare(services, where)
            vulns = _drop_unplantable_bare(vulns, where)
            # REQUIRED_VARS mirrors the catalog's required_vars incompletely; also drop what it declares.
            banned = catalog_required_var_names()
            services = _drop_catalog_var_bare(services, where, banned)
            vulns = _drop_catalog_var_bare(vulns, where, banned)
            box_configs[box["name"]] = (services, vulns)

        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        (comp_dir / "box_vulns.json").write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )


    # Pin gates at generate time, not mid-plant; on the reuse path a recorded pin exempts the box.
    baseline_path = comp_dir / "box_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    for box_name, (services, vulns) in box_configs.items():
        configs = list(services) + list(vulns) + list(baseline.get(box_name, []))
        where = f"pins for '{box_name}'"
        _validate_pin_vars(configs, where)
        _validate_windows_local_user_pins(configs, where)
        reused_names = {_config_name(c) for c in configs} if reused else frozenset()
        _validate_known_broken_pins(configs, where, exempt=reused_names)

    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        if is_unmanaged(box):
            continue
        services, vulns = box_configs[box["name"]]
        # Domain pins re-inject with real vars; PIN_CHECK_OVERRIDES strips keys, keeps the config.
        configurations = []
        for c in services + vulns + list(baseline.get(box["name"], [])):
            name = c if isinstance(c, str) else c["name"]
            if name in DOMAIN_INFRA_CONFIGS:
                continue
            if isinstance(c, dict) and c.get("score_only"):
                continue
            configurations.append(
                c if isinstance(c, str)
                else {k: v for k, v in c.items()
                      if k not in PIN_CHECK_OVERRIDES and k != "plant_only"})
        configurations.sort(
            key=lambda c: (c if isinstance(c, str) else c["name"]) in DISRUPTIVE_CONFIGS
        )
        windows = is_windows_template(box["template"])
        machines.append({
            "id": i,
            "name": f"{box['name']}-team{team['identifier']}",
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "os": box["template"],
            "user": WINDOWS_ADMIN_USER if windows else box_username,
            "password": box_password,
            "configurations": configurations,
        })
    for machine in machines:
        _fill_identity_vars(machine, machines)

    config_path = comp_dir / "nakon-config.json"
    config_path.write_text(json.dumps({"machines": machines}, indent=2))
    os.chmod(config_path, 0o600)
    return config_path


def _golden_stage_machines(full, unbooted, anchor_identifier, box_index_by_name):
    # One machine per box type at the anchor's golden IP; anchor must sit on a bridge the builder has.
    identity_banned = _identity_banned_configs()
    seen_types = set()
    golden_machines = []
    for m in full:
        box_name = m["name"].rsplit("-team", 1)[0]
        if (m["ip"].split(".")[2] != str(anchor_identifier) or box_name in seen_types
                or box_name in unbooted):
            continue
        seen_types.add(box_name)
        box_idx = box_index_by_name[box_name]
        late = _late_mode_names([m])
        golden_kept = [c for c in m["configurations"]
                       if (c if isinstance(c, str) else c["name"]) not in POST_CLONE_CONFIGS
                       and _config_name(c) not in late
                       and not is_domain_dependent(
                           c if isinstance(c, str) else c["name"])]
        banned = sorted({(c if isinstance(c, str) else c["name"]) for c in golden_kept}
                        & identity_banned)
        if banned:
            raise SystemExit(
                f"  ERROR: identity-dependent config(s) {banned} would ride the golden "
                f"disk of '{box_name}' and clone the golden box's address into every "
                f"team. Add them to PREDOMAIN_STAGE_CONFIGS or FINAL_STAGE_CONFIGS "
                f"(constants.py) so they plant per machine post-clone.")
        golden_machines.append({
            **m,
            "id": len(golden_machines) + 1,
            "name": f"{box_name}-golden",
            "ip": f"192.168.{anchor_identifier}.{GOLDEN_IP_BASE + box_idx}",
            "configurations": golden_kept,
        })
    return golden_machines


def generate_slot_golden_config(comp_dir, boxes, unbooted, anchor_identifier, slot):
    full = json.loads((comp_dir / "nakon-config.json").read_text())["machines"]
    box_index_by_name = {b["name"]: i for i, b in enumerate(boxes)}
    machines = _golden_stage_machines(full, unbooted, anchor_identifier,
                                      box_index_by_name)
    path = comp_dir / f".nakon-golden-slot{slot}.json"
    path.write_text(json.dumps({"machines": machines}, indent=2))
    os.chmod(path, 0o600)
    return path


def generate_stage_configs(comp_dir, teams, boxes, unbooted=frozenset()):
    # unbooted box types (DCs) get no golden machine and plant per team in the pre-domain stage.
    full = json.loads((comp_dir / "nakon-config.json").read_text())["machines"]
    team1_identifier = str(teams["team1"]["identifier"])
    box_index_by_name = {b["name"]: i for i, b in enumerate(boxes)}

    def config_name(c):
        return c if isinstance(c, str) else c["name"]

    golden_machines = _golden_stage_machines(full, unbooted, team1_identifier,
                                             box_index_by_name)
    # Domain-dependent pins plant post-domain: else a DC's whole plan rides the pre-domain pass.
    domain_dependent = {config_name(c) for m in full for c in m["configurations"]
                        if is_domain_dependent(config_name(c))}
    post_clone = POST_CLONE_CONFIGS | domain_dependent
    late_names = _late_mode_names(full)

    def stage_machines(want, include_golden_stage=False):
        out = []
        for m in full:
            cold = m["name"].rsplit("-team", 1)[0] in unbooted
            kept = []
            for c in m["configurations"]:
                name = config_name(c)
                golden_stage = name not in post_clone and name not in late_names
                if name not in want and not (include_golden_stage and cold and golden_stage):
                    continue
                required = REQUIRED_VARS.get(name) or {}
                if required:
                    # Machine identity wins: these fills override the pin's own vars below.
                    filled = {v: m["ip"] for v, kind in required.items() if kind == "ip"}
                    for v, kind in required.items():
                        if str(kind).startswith("ip:"):
                            filled[v] = _cross_box_ip(full, m["name"],
                                                      str(kind).split(":", 1)[1])
                    if filled:
                        pinned = (c.get("vars") or {}) if isinstance(c, dict) else {}
                        c = {"name": name, "vars": {**pinned, **filled}}
                kept.append(c)
            if not kept:
                continue
            out.append({**m, "configurations": kept})
        return out

    predomain_machines = stage_machines(PREDOMAIN_STAGE_CONFIGS, include_golden_stage=True)
    final_machines = stage_machines(FINAL_STAGE_CONFIGS | domain_dependent)
    late_machines = stage_machines(late_names)
    # The late pass is excluded here: redeploy's sweep replays it, and hardening would clobber it.
    combined = {}
    for m in predomain_machines + final_machines:
        entry = combined.setdefault(m["name"], {**m, "configurations": []})
        seen = {_pin_key(c) for c in entry["configurations"]}
        for c in m["configurations"]:
            key = _pin_key(c)
            if key in seen:
                continue
            seen.add(key)
            entry["configurations"].append(c)
    postclone_machines = sorted(combined.values(), key=lambda m: m["name"])

    written = []
    for suffix, machines in ((".nakon-golden.json", golden_machines),
                             (".nakon-predomain.json", predomain_machines),
                             (".nakon-final.json", final_machines),
                             (".nakon-postclone.json", postclone_machines),
                             (".nakon-late.json", late_machines)):
        path = comp_dir / suffix
        path.write_text(json.dumps({"machines": machines}, indent=2))
        os.chmod(path, 0o600)
        written.append(path)
    return tuple(written)
