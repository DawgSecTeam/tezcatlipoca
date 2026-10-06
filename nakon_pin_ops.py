"""Pin/var validation and identity-var filling for nakon machine configs."""

import json

from constants import KNOWN_BROKEN_CONFIGS, REQUIRED_VARS


def _config_name(c):
    return c if isinstance(c, str) else c["name"]


def _pin_key(c):
    """Identity of one pin for dedup: (config name, canonical vars).

    Same-named pins with different vars are DIFFERENT plants — box_baseline.json
    emits 3+ same-named local-user/local-user-win decoy accounts per box, and a cold
    box's otherwise golden-stage configs ride the repair stage into the same combined
    list. Keying on the name alone silently collapsed them (audit-found 2026-10-02:
    .nakon-postclone.json — what redeploy's convergence sweep deploys and verify
    --packet probes — kept 2 of the 4 decoy/plant pins, so a rollback-base sweep
    stopped re-planting packet-promised accounts)."""
    return (_config_name(c),
            json.dumps(c.get("vars") or {}, sort_keys=True) if isinstance(c, dict) else "")


def _cross_box_ip(machines, machine_name, target_box):
    """Same team's copy of target_box's IP, for REQUIRED_VARS "ip:<box>" vars."""
    _, _, identifier = machine_name.rpartition("-team")
    for other in machines:
        box, _, team = other["name"].rpartition("-team")
        if box == target_box and team == identifier:
            return other["ip"]
    raise SystemExit(
        f"  ERROR: cross-box var wants {target_box}'s IP for '{machine_name}' but no "
        f"machine '{target_box}-team{identifier}' exists in nakon-config.json — check "
        f"the box name in the REQUIRED_VARS \"ip:<box>\" kind.")


def _is_identity_kind(kind):
    return str(kind) == "ip" or str(kind).startswith("ip:")


def _identity_banned_configs():
    """Config names whose REQUIRED_VARS carry any identity-dependent kind.

    The single source of truth for BOTH golden paths — slot 0 (generate_stage_configs)
    and every satellite (generate_slot_golden_config). A config whose var is the
    machine's own address ("ip") or a same-team box's address ("ip:<box>") writes that
    address into the disk, so riding a golden bakes the golden's address into every
    linked clone. _golden_stage_machines consults this directly; the two public paths
    can no longer disagree. audit-found 2026-10-02: the satellite path tested
    `"ip" in kinds.values()`, so a cross-box-only ("ip:<box>") config escaped the ban
    while _fill_identity_vars had already baked team1's database IP into the pin —
    every clone on that satellite would have pointed at team1's database. It was
    masked only because every current "ip:<box>" config also happens to be in
    REPAIR_STAGE_CONFIGS."""
    return {name for name, kinds in REQUIRED_VARS.items()
            if any(_is_identity_kind(kind) for kind in kinds.values())}


def _fill_identity_vars(machine, machines):
    """Fill REQUIRED_VARS identity vars in the BASE machine list (generate time).

    The stage-file generation fills per stage too, but the bundle is built from THIS
    list — a config whose script reads an identity var (airship-webapp's $DB_HOST)
    needs it declared here or the bundle lint rejects the whole build."""
    for c in machine["configurations"]:
        if isinstance(c, str):
            continue
        required = REQUIRED_VARS.get(c["name"]) or {}
        if not required:
            continue
        vars_ = c.setdefault("vars", {})
        for var, kind in required.items():
            if not _is_identity_kind(kind):
                continue
            if str(kind) == "ip":
                vars_[var] = machine["ip"]
            else:
                vars_[var] = _cross_box_ip(machines, machine["name"],
                                           str(kind).split(":", 1)[1])


def _validate_pin_vars(configurations, where):
    """Reject bare-name (or var-incomplete) selections of configs that require vars.

    The catalog DB carries no required-vars metadata, so REQUIRED_VARS is the curated
    operator-level source of truth; this turns a mid-plant rc=2 into a generate-time
    error with the exact fix. Identity ("ip") vars are exempt here — they are filled
    per machine at stage-file generation, and their configs are banned from the golden
    stage there."""
    for c in configurations:
        name = _config_name(c)
        required = REQUIRED_VARS.get(name)
        if not required:
            continue
        literals = [v for v, kind in required.items() if kind == "literal"]
        if isinstance(c, str):
            if literals:
                raise SystemExit(
                    f"  ERROR: '{name}' requires var(s) {', '.join(literals)} but is pinned "
                    f"as a bare name in {where}. Pin it as {{\"name\": \"{name}\", \"vars\": {{...}} }} "
                    f"in box_services.json / box_vulns.json — a bare-name plant fails mid-deploy "
                    f"(sudoers-rule rc=2, live-found 2026-09-24).")
        else:
            provided = c.get("vars") or {}
            missing = [v for v in literals if v not in provided]
            if missing:
                raise SystemExit(
                    f"  ERROR: '{name}' is pinned with vars but missing {', '.join(missing)} "
                    f"in {where} — add them to the \"vars\" object.")


def _validate_known_broken_pins(configurations, where, exempt=frozenset()):
    """Reject a *new* pin of a catalog config with a live-confirmed defect.

    constants.KNOWN_BROKEN_CONFIGS is the curated, machine-readable replacement for the
    prose + per-competition-JSON pruning that let a new comp silently re-pin one of these
    (tftpd's Noble dpkg wedge, sshd-force-sftp killing SSH, the Windows user-policy set).
    `exempt` is the escape hatch for a competition that already records the pin — the
    historical comps that predate this gate (cde-2026, pfsense-rvb, scrim-live all pin
    local-user-win) must stay re-deployable; they get a warning instead of an error."""
    for c in configurations:
        name = _config_name(c)
        reason = KNOWN_BROKEN_CONFIGS.get(name)
        if not reason:
            continue
        if name in exempt:
            print(f"  WARNING: '{name}' is a known-broken catalog config, kept because this "
                  f"competition already records it ({where}): {reason}")
            continue
        raise SystemExit(
            f"  ERROR: '{name}' is a known-broken catalog config and cannot be pinned "
            f"({where}): {reason} Unpin it from box_vulns.json/box_services.json "
            f"(the list lives in constants.KNOWN_BROKEN_CONFIGS; it shrinks as upstream "
            f"fixes land — see docs/upstream-defects-handoff.md).")


def _bare_unplantable_reason(name):
    """Why a *freshly randomized* bare-name pin cannot be planted, or None if it can.

    Two classes: a known-broken catalog config (constants.KNOWN_BROKEN_CONFIGS) and a config
    whose REQUIRED_VARS include a literal — randomize returns bare names with no vars, so the
    generate-time var gate below would abort the whole run for a pin nobody chose."""
    if name in KNOWN_BROKEN_CONFIGS:
        return "known-broken"
    required = REQUIRED_VARS.get(name)
    if required and any(kind == "literal" for kind in required.values()):
        return "requires operator vars"
    return None


def _drop_unplantable_bare(names, where):
    """Drop unplantable bare names from a freshly randomized selection, with a notice.

    nakon's randomize (>= d5a443a) already skips configs whose vars it cannot satisfy, but the
    vendored copy may predate that and it has no knowledge of the driver-side broken table, so
    filter here rather than aborting `create-competition` mid-flow. Recorded pins (reuse path)
    are still gated by the two validators below."""
    kept, dropped = [], []
    for c in names:
        name = _config_name(c)
        reason = _bare_unplantable_reason(name)
        if reason:
            dropped.append((name, reason))
        else:
            kept.append(c)
    if dropped:
        detail = "; ".join(f"{n} ({r})" for n, r in dropped)
        print(f"  Skipping {len(dropped)} unplantable bare config(s) in {where}: {detail} — "
              f"pin them explicitly with vars, or see constants.KNOWN_BROKEN_CONFIGS / "
              f"docs/upstream-defects-handoff.md")
    return kept
