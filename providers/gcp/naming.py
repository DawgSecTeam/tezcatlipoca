"""Names and ownership labels for a GCP range's resources.

One VPC per range is the isolation unit: its NAME carries the comp+run identity
(VPCs take no labels), everything inside it carries the same set as LABELS. The
roster's positional unit number (the proxmox 'vmid') stays the pipeline's stable
unit key and rides the resource name, so two concurrent ranges of one competition
never collide on instance or image names.
"""

import re

from common.constants import comp_slug

# GCE: 63 chars max, lowercase letters/digits/hyphens, must start with a letter.
_NAME_MAX = 63


def run_hex(run_id):
    return "".join(c for c in str(run_id or "").lower().removeprefix("run-")
                   if c in "0123456789abcdef")[:8]


def _clean(name):
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if len(cleaned) > _NAME_MAX:
        raise SystemExit(
            f"  ERROR: the GCP resource name '{name}' is too long ({len(cleaned)}/{_NAME_MAX}) "
            f"— picked for {cleaned!r}. Shorten the competition name or box names; a "
            f"silent truncate would orphan the resource from its recorded name.")
    if not cleaned:
        raise SystemExit(f"  ERROR: resource name {name!r} has no usable characters")
    if not cleaned[0].isalpha():
        raise SystemExit(f"  ERROR: GCP names must start with a letter: {cleaned!r}")
    return cleaned


def prefix(comp_name, run_id):
    """Every resource of this range: tz-<comp-slug>-<run8>."""
    return _clean(f"tz-{comp_slug(comp_name)}-{run_hex(run_id)}")


def instance_name(prefix, vm_name):
    return _clean(f"{prefix}-{vm_name}")


def image_name(prefix, role, box=""):
    # engine template + goldens are per-competition caches, kept across --teams-only runs
    return _clean(f"{prefix}-{role}{'-' + box if box else ''}-img")


def machine_image_name(prefix, vm_name, mark):
    return _clean(f"{prefix}-{vm_name}-{mark}")


def vpc_name(prefix):
    return _clean(prefix)


def subnet_name(prefix, role, identifier=""):
    return _clean(f"{prefix}-net-{role}{identifier}")


def firewall_name(prefix, role, identifier=""):
    return _clean(f"{prefix}-fw-{role}{identifier}")


def router_name(prefix):
    return _clean(f"{prefix}-router")


def nat_name(prefix):
    return _clean(f"{prefix}-nat")


def team_tag(identifier):
    """The per-team network tag: the intra-range firewall rules scope by it, so a
    cross-team packet has no allow anywhere on its path (the engine's iptables FORWARD
    DROP stays as the second gate)."""
    return _clean(f"tz-team-{identifier}")


def route_name(prefix):
    return _clean(f"{prefix}-route-engine")


def route_wan_name(prefix):
    return _clean(f"{prefix}-route-engine-wan")


def address_name(prefix, role):
    return _clean(f"{prefix}-ip-{role}")


# The label equivalent of the PVE ownership tags: the same destruction-identity set.
LABEL_RANGE = "tz-range"
LABEL_COMP = "tz-comp"
LABEL_RUN = "tz-run"
LABEL_ROLE = "tz-role"
# The roster's positional unit (the proxmox 'vmid') rides the name AND this label, so
# resume guards can answer "is the recorded unit live?" without a vmid-shaped API.
LABEL_UNIT = "tz-unit"

_ALL = object()


def labels(comp_name, run_id, role=None, unit=None):
    """The ownership proof every destroy path compares (PVE: tags; GCP: labels)."""
    values = {LABEL_RANGE: "1", LABEL_COMP: comp_slug(comp_name)}
    if run_id:
        values[LABEL_RUN] = run_hex(run_id)
    if role:
        values[LABEL_ROLE] = role
    if unit is not None:
        values[LABEL_UNIT] = str(int(unit))
    return values


def owned(info, comp_name, run_id=None, role=_ALL):
    """True when a listed resource's own labels prove this run's ownership.

    `role=_ALL` accepts a role-free match (the clone sweep); a role narrows it
    (template reclaims), mirroring infra.ownership's expectations.
    """
    found = info.get("labels") or {}
    if found.get(LABEL_COMP) != comp_slug(comp_name) or found.get(LABEL_RANGE) != "1":
        return False
    if run_id and found.get(LABEL_RUN) != run_hex(run_id):
        return False
    if role is not _ALL and found.get(LABEL_ROLE) != role:
        return False
    return True


def unproven(info, comp_name):
    """A resource carrying this competition's label but not this run's: another range's."""
    found = info.get("labels") or {}
    return found.get(LABEL_COMP) == comp_slug(comp_name)
