"""Pin/var validation and identity-var filling for nakon machine configs."""

import json
import re
import subprocess
import sys

from common.constants import KNOWN_BROKEN_CONFIGS, NAKON_DIR, REQUIRED_VARS


def _config_name(c):
    return c if isinstance(c, str) else c["name"]


def _pin_key(c):
    # (config name, canonical vars): same-named pins with different vars are DIFFERENT plants
    # (box_baseline.json emits 3+ same-named decoy accounts per box), and keying on name collapsed them.
    return (_config_name(c),
            json.dumps(c.get("vars") or {}, sort_keys=True) if isinstance(c, dict) else "")


def _cross_box_ip(machines, machine_name, target_box):
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
    # Config names whose REQUIRED_VARS carry any identity-dependent kind. The single source of
    # truth for BOTH golden paths; a config writing an address bakes it into every linked clone.
    return {name for name, kinds in REQUIRED_VARS.items()
            if any(_is_identity_kind(kind) for kind in kinds.values())}


def _fill_identity_vars(machine, machines):
    # The bundle is built from THIS base list, and its lint rejects a config reading an undeclared identity var.
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


def _windows_password_error(username, password, fullname=""):
    if not password:
        return None
    if len(password) < 8:
        return "password must be at least 8 characters"
    if sum(bool(re.search(pattern, password)) for pattern in (r"[A-Z]", r"[a-z]", r"\d", r"[^A-Za-z0-9]")) < 3:
        return "password must contain at least three of uppercase, lowercase, digit, and symbol"
    lowered = password.casefold()
    if username and username.casefold() in lowered:
        return "password must not contain the username"
    for token in re.findall(r"[A-Za-z0-9]+", fullname or ""):
        for i in range(len(token) - 2):
            if token[i:i + 3].casefold() in lowered:
                return "password must not contain three consecutive characters from the full name"
    return None


def _validate_windows_local_user_pins(configurations, where):
    for c in configurations:
        if not isinstance(c, dict) or c.get("name") != "local-user-win":
            continue
        vars_ = c.get("vars") or {}
        reason = _windows_password_error(vars_.get("USERNAME", ""), vars_.get("PASSWORD", ""), vars_.get("FULLNAME", ""))
        if reason:
            raise SystemExit(
                f"  ERROR: local-user-win on {where} for {vars_.get('USERNAME', '<unknown>')!r}: {reason}")


def _validate_pin_vars(configurations, where):
    # Reject bare-name (or var-incomplete) selections of configs that require vars: turns a mid-plant
    # rc=2 into a generate-time error. Identity ("ip") vars are exempt — filled per machine at staging.
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
                    f"(sudoers-rule rc=2, caught live).")
        else:
            provided = c.get("vars") or {}
            missing = [v for v in literals if v not in provided]
            if missing:
                raise SystemExit(
                    f"  ERROR: '{name}' is pinned with vars but missing {', '.join(missing)} "
                    f"in {where} — add them to the \"vars\" object.")


def _validate_known_broken_pins(configurations, where, exempt=frozenset()):
    # Reject a NEW pin of a catalog config with a live-confirmed defect. `exempt` lets a
    # competition that already records the pin stay re-deployable (warning, not error).
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
            f"fixes land).")


def catalog_required_var_names(timeout=180):
    # Config names the nakon catalog itself declares required_vars for (the curated table is an
    # incomplete mirror). Cached per process; frozenset() (fail-open) when the catalog is unreachable.
    global _CATALOG_VAR_NAMES
    if _CATALOG_VAR_NAMES is None:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "nakon", "catalog", "list", "--json"],
                cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=timeout)
            data = json.loads(proc.stdout)
            _CATALOG_VAR_NAMES = frozenset(
                e.get("name") for e in data if e.get("name") and e.get("required_vars"))
        except Exception:
            _CATALOG_VAR_NAMES = frozenset()
    return _CATALOG_VAR_NAMES


_CATALOG_VAR_NAMES = None


def _drop_catalog_var_bare(names, where, banned):
    kept, dropped = [], []
    for c in names:
        name = _config_name(c)
        has_vars = isinstance(c, dict) and bool(c.get("vars"))
        if name in banned and not has_vars:
            dropped.append(name)
        else:
            kept.append(c)
    if dropped:
        print(f"  Skipping {len(dropped)} catalog-var bare config(s) in {where}: "
              f"{', '.join(sorted(set(dropped)))} — the catalog declares required_vars, "
              f"randomize cannot satisfy them; pin explicitly with vars")
    return kept


def _bare_unplantable_reason(name):
    # Why a freshly randomized bare-name pin cannot be planted, or None: known-broken, or a
    # config whose REQUIRED_VARS include a literal (randomize returns bare names with no vars).
    if name in KNOWN_BROKEN_CONFIGS:
        return "known-broken"
    required = REQUIRED_VARS.get(name)
    if required and any(kind == "literal" for kind in required.values()):
        return "requires operator vars"
    return None


def _drop_unplantable_bare(names, where):
    # nakon's randomize (>= d5a443a) already skips configs whose vars it cannot satisfy, but a
    # vendored copy may predate it and knows nothing of the broken table, so filter here.
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
              f"pin them explicitly with vars, or see constants.KNOWN_BROKEN_CONFIGS")
    return kept
