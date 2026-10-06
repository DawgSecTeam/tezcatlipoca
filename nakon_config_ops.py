"""Generate nakon machine lists: randomized config, per-slot golden config, staged configs."""

import json
import math
import os
import subprocess
import sys

from constants import (
    DISRUPTIVE_CONFIGS,
    DOMAIN_INFRA_CONFIGS,
    FINAL_STAGE_CONFIGS,
    GOLDEN_IP_BASE,
    NAKON_DIR,
    PIN_CHECK_OVERRIDES,
    POST_CLONE_CONFIGS,
    REPAIR_STAGE_CONFIGS,
    REQUIRED_VARS,
    SLOW_SERVICES,
    is_domain_dependent,
    WINDOWS_ADMIN_USER,
)
from utils import is_unmanaged
from windows_ops import is_windows_template
from nakon_pin_ops import (
    _config_name,
    _pin_key,
    _cross_box_ip,
    _identity_banned_configs,
    _fill_identity_vars,
    _validate_pin_vars,
    _validate_known_broken_pins,
    _drop_unplantable_bare,
)


def os_to_platform(template):
    """Classify a free-text template name the way nakon does: 'windows' if it has 'win'."""
    return "windows" if "win" in template.lower() else "linux"


def _nakon_randomize(platform, services_budget, vulns_budget):
    """Pick services+vulns via `nakon randomize --json` (cwd=NAKON_DIR for catalog access)."""
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
            # randomize cannot know this driver-side broken list, so drop those names here
            # rather than aborting the run in the generate-time gate below.
            where = f"fresh selection for '{box['name']}'"
            services = _drop_unplantable_bare(services, where)
            vulns = _drop_unplantable_bare(vulns, where)
            box_configs[box["name"]] = (services, vulns)

        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        (comp_dir / "box_vulns.json").write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )


    # Pin gates, at generate time instead of mid-plant:
    #   - var gate (M4): bare-name selections of var-requiring configs (rc=2/rc=127);
    #   - known-broken gate: constants.KNOWN_BROKEN_CONFIGS, the machine-readable
    #     replacement for the per-competition-JSON pruning.
    # Baseline pins (box_baseline.json — packet-promised decoy/local accounts) plant like
    # vulns and get the same gates. On the reuse path every pin is one this competition
    # already records, so the known-broken gate exempts the box (historical comps stay
    # re-deployable); a fresh randomize selection has nothing recorded and is refused.
    baseline_path = comp_dir / "box_baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    for box_name, (services, vulns) in box_configs.items():
        configs = list(services) + list(vulns) + list(baseline.get(box_name, []))
        where = f"pins for '{box_name}'"
        _validate_pin_vars(configs, where)
        reused_names = {_config_name(c) for c in configs} if reused else frozenset()
        _validate_known_broken_pins(configs, where, exempt=reused_names)

    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        if is_unmanaged(box):
            continue  # firewall/appliance: no nakon plant (see utils.is_unmanaged)
        services, vulns = box_configs[box["name"]]
        # Three kinds of pin never ride the machine list:
        #   DOMAIN_INFRA_CONFIGS — domain_ops re-injects them per team with real vars;
        #   score-only pins      — scoring constructs with no catalog config (native
        #                          services like AD's own DNS); quotient/setup builds
        #                          their event.conf check directly;
        #   PIN_CHECK_OVERRIDES  — scoring-only keys on a real plant (strip the keys,
        #                          keep the config). The box_services.json write-back
        #                          above keeps them, so push_event_conf still sees them
        #                          later in the deploy.
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
    """One machine per box type at the anchor subnet's golden IP. The anchor is
    team1 for slot 0 (historical math) and the satellite's first local team for
    satellite slots — the golden must sit on a bridge that exists on the host
    building it, with that node's jump routing the engine's plant there.

    The identity ban is computed here, not passed in, so slot 0 and every satellite
    share one definition (_identity_banned_configs) and cannot drift apart."""
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
        golden_kept = [c for c in m["configurations"]
                       if (c if isinstance(c, str) else c["name"]) not in POST_CLONE_CONFIGS
                       and not is_domain_dependent(
                           c if isinstance(c, str) else c["name"])]
        banned = sorted({(c if isinstance(c, str) else c["name"]) for c in golden_kept}
                        & identity_banned)
        if banned:
            raise SystemExit(
                f"  ERROR: identity-dependent config(s) {banned} would ride the golden "
                f"disk of '{box_name}' and clone the golden box's address into every "
                f"team. Add them to REPAIR_STAGE_CONFIGS or FINAL_STAGE_CONFIGS "
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
    """The satellite slot's golden-stage config: identical planted content to slot 0
    (per-box golden hashes stay identical across slots), different transport IPs —
    the anchor team's subnet, reachable from the engine via the jump. Returns the
    path (0600, per-run-secret class)."""
    full = json.loads((comp_dir / "nakon-config.json").read_text())["machines"]
    box_index_by_name = {b["name"]: i for i, b in enumerate(boxes)}
    machines = _golden_stage_machines(full, unbooted, anchor_identifier,
                                      box_index_by_name)
    path = comp_dir / f".nakon-golden-slot{slot}.json"
    path.write_text(json.dumps({"machines": machines}, indent=2))
    os.chmod(path, 0o600)
    return path


def generate_stage_configs(comp_dir, teams, boxes, unbooted=frozenset()):
    """Split the full nakon config into golden / repair / final stage configs (M3.2).

    nakon-config.json (all teams, full configuration lists) stays the source of truth.
    The golden stage takes ONE machine per box type — team1's copy of the full list minus
    the post-clone subsets, placed at the golden IP on team1's subnet — because the golden
    set exists so linked clones inherit the heavy work (package installs, services,
    non-disruptive misconfigs) as bytes on disk. The repair stage (phase 5, before
    domains) and the final stage (phase 6, after domains — see constants for why the
    split matters) carry every team machine with their own subsets; machines with an
    empty subset for a stage are dropped from that stage's file. A combined post-clone
    view (repair ∪ final) is also written for redeploy's convergence sweeps. Returns
    (golden_path, repair_path, final_path, postclone_path); all 0600 and gitignored
    (per-run-secret class — they carry box_password).

    unbooted: box types whose golden stays generalized (domain controllers — see
    golden_ops' identity note). They get no golden machine; everything that would have
    ridden their golden plants per team in the repair stage instead (still pre-domain)."""
    full = json.loads((comp_dir / "nakon-config.json").read_text())["machines"]
    team1_identifier = str(teams["team1"]["identifier"])
    box_index_by_name = {b["name"]: i for i, b in enumerate(boxes)}

    def config_name(c):
        return c if isinstance(c, str) else c["name"]

    # Identity-dependent configs (REQUIRED_VARS "ip"/"ip:<box>" kinds) write an
    # address into the disk. Planted on the golden they would bake the golden's IP
    # into every linked clone, so _golden_stage_machines refuses them there and the
    # repair/final stages fill them per machine instead (see
    # _identity_banned_configs for why this is one shared definition).

    golden_machines = _golden_stage_machines(full, unbooted, team1_identifier,
                                             box_index_by_name)
    # Domain-dependent configs (ad-* / GPO / the explicit final-stage set) plant per
    # team AFTER the domain pass; they are never golden- or repair-stage, whatever a
    # pin said (a DC box type is unbooted, so absent this rule its whole plan would
    # ride the pre-promotion repair sweep and every domain-dependent step would fail).
    domain_dependent = {config_name(c) for m in full for c in m["configurations"]
                        if is_domain_dependent(config_name(c))}
    post_clone = POST_CLONE_CONFIGS | domain_dependent

    def stage_machines(want, include_golden_stage=False):
        out = []
        for m in full:
            cold = m["name"].rsplit("-team", 1)[0] in unbooted
            kept = []
            for c in m["configurations"]:
                name = config_name(c)
                golden_stage = name not in post_clone
                if name not in want and not (include_golden_stage and cold and golden_stage):
                    continue
                required = REQUIRED_VARS.get(name) or {}
                if required:
                    # Machine identity wins: fill/overwrite "ip"-kind vars with THIS
                    # machine's address, and "ip:<box>"-kind vars with the same team's
                    # copy of that box — whatever the pin said.
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

    repair_machines = stage_machines(REPAIR_STAGE_CONFIGS, include_golden_stage=True)
    final_machines = stage_machines(FINAL_STAGE_CONFIGS | domain_dependent)
    # Merge repair ∪ final per machine, deduping on the pin's full identity
    # (_pin_key: name + vars) rather than its name alone — a name-only collapse drops
    # packet-promised same-named decoy accounts (audit-found 2026-10-02).
    combined = {}
    for m in repair_machines + final_machines:
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
                             (".nakon-repair.json", repair_machines),
                             (".nakon-final.json", final_machines),
                             (".nakon-postclone.json", postclone_machines)):
        path = comp_dir / suffix
        path.write_text(json.dumps({"machines": machines}, indent=2))
        os.chmod(path, 0o600)
        written.append(path)
    return tuple(written)
