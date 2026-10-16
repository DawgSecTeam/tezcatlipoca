"""The per-competition bad-auto config: red01's identity, reserved and isolated.

Every tezcatlipoca worktree resolves the SAME sibling bad-auto checkout
(``REPO.parent/"bad-auto"``), and until 2026-10-12 bad-auto's deploy identity
lived in that checkout's repo-root files — one ``config.yaml`` plus one
``.deploy-stamp.json`` shared by every session. A validation deploy following
the shared config recycled vmid 999 and killed another session's in-flight
red01 mid-event; the 3-way identity check validated the wrong owner over state
the recycler had just rewritten (it was correct logic over globally clobbered
state).

This module is the fix's anchor: the competition's bad-auto config lives in
its OWN directory (``competitions/<id>/.badauto-config.yaml``, like teams.json
— per-deploy identity), bad-auto stamps ``.deploy-stamp.json`` next to that
config, and red01's vmid/IP are reserved per competition and gated by the
preflight like the scoring engine's. bad-auto's repo-root ``config.yaml``
remains for standalone bad-auto use; tezcatlipoca never reads or writes it.

The realm-C2 flow's derived-config pattern (``realm_c2_ops``) is generalized
here rather than duplicated: the realm block is one layer this preparer can
apply, exactly as ``prepare_local_c2`` did to the sibling singleton.
"""

import json
from pathlib import Path

import yaml

from common.constants import GOLDEN_VMID_OFFSET, MAX_BOXES_PER_TEAM
from common.utils import compfile_value, competition_name

# The one file name for a competition's bad-auto config. YAML on disk (JSON
# parses as YAML, so hand-written JSON in it also loads).
CONFIG_NAME = ".badauto-config.yaml"

# The pre-placement address: read only as a v1 document's recorded value or a scrim's
# last-resort fallback. A fresh range SELECTS its address (infra.collisions.select_red_ip).
DEFAULT_RED01_IP = "10.0.0.198"


def default_red_vmid(engine_vmid, slots_used):
    """red01's derived vmid: one golden stride past this placement's last golden band."""
    # The old +170 offset landed exactly on slot-2 box 0 of a 10-box default range: one
    # stride past the goldens is clear of every slot's band for any MAX_BOXES_PER_TEAM.
    return int(engine_vmid) + GOLDEN_VMID_OFFSET + (int(slots_used) + 1) * MAX_BOXES_PER_TEAM


def config_path(comp_dir):
    """The competition's bad-auto config path (absolute — bad-auto runs with
    cwd=<its own checkout>, so a relative --config would resolve in ITS tree
    and load_config would silently fall back to built-in DEFAULTS: red01 at
    red_vmid 999 / 10.0.0.199 while every other component targeted this comp)."""
    return (Path(comp_dir) / CONFIG_NAME).resolve()


def load_config(comp_dir):
    """The competition's bad-auto config dict, or {} when none exists yet.

    Accepts JSON or YAML on disk (the harness used to write JSON under this
    role; the preparer writes YAML)."""
    path = config_path(comp_dir)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text) or {}
    except ValueError:
        return yaml.safe_load(text) or {}


def write_config(comp_dir, cfg):
    """Write the competition's bad-auto config atomically; returns the ABSOLUTE
    path for `--config` threading (see config_path for why absolute)."""
    # bad-auto's red01 description marker takes its comp= from deploy.comp_name. A dashboard
    # run's comp dir is literally <run_dir>/comp, so the directory-basename fallback tagged
    # every dashboard range "comp" while this plan's tags carried the Compfile name — and
    # infra/red01.py's resume gate compares the two, so it refused this competition's OWN
    # live red01. Stamped here, where every writer lands, so the two cannot disagree.
    cfg["deploy"] = {**(cfg.get("deploy") or {}), "comp_name": competition_name(comp_dir)}
    path = config_path(comp_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def existing_blackbox(comp_dir):
    """Operator/club blocks carried across runs: realm, beacon, evasion, naming
    and the operator's deploy keys. Read from the COMPETITION'S OWN prior
    config — never bad-auto's repo-root singleton, which on a shared checkout
    names whichever session staged last (that carry is how a run's realm block
    used to survive; per-comp it survives better)."""
    return load_config(comp_dir)


def _placement_of(comp_dir):
    from multinode.placement_record import read_placement
    return read_placement(comp_dir)


def red01_vmid_override(comp_dir):
    """The operator's reservation: Compfile `red01_vmid <n>` or the comp config's
    deploy.red_vmid, else None (never a derived value).

    There is deliberately NO fall-through to bad-auto's built-in 999 — a shared
    default vmid is a shared node slot, and sharing it is the 2026-10-12 kill."""
    raw = compfile_value(Path(comp_dir) / "Compfile", "red01_vmid", "")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    cfgd = ((load_config(comp_dir).get("deploy") or {}).get("red_vmid"))
    if cfgd:
        try:
            return int(cfgd)
        except (TypeError, ValueError):
            pass
    return None


def red01_vmid_for(comp_dir, engine_vmid=None, placement=None):
    """red01's vmid: the operator's override, the recorded placement, else the stride.

    The placement is authoritative once written: a resume and every companion op must see the
    slot the range was actually deployed at."""
    override = red01_vmid_override(comp_dir)
    if override is not None:
        return override
    placement = _placement_of(comp_dir) if placement is None else placement
    recorded = ((placement or {}).get("red01") or {}).get("vmid")
    if recorded:
        return int(recorded)
    if engine_vmid is None:
        return None
    return default_red_vmid(engine_vmid, len((placement or {}).get("satellites") or []))


def red01_vmid_legacy(comp_dir, engine_vmid):
    """The pre-placement derivation (override, else engine+170) — a v1 document's own range."""
    override = red01_vmid_override(comp_dir)
    return override if override is not None else int(engine_vmid) + 170


def red01_ip_override(comp_dir):
    """The operator's address (Compfile `red01_ip`, legacy `assume_breach_red_ip`), else ''."""
    compfile = Path(comp_dir) / "Compfile"
    return (compfile_value(compfile, "red01_ip", "")
            or compfile_value(compfile, "assume_breach_red_ip", "") or "")


def red01_ip_is_explicit(comp_dir):
# Whether the address came from the operator (Compfile) rather than from selection: an
#     explicit address keeps the "unverifiable is not free" refusal (infra/red01.py); a
#     selected one was proven free when it was chosen.
    return bool(red01_ip_override(comp_dir))


def red01_ip_for(comp_dir, placement=None):
    """red01's address: the operator's, the placement's recorded one, else ''.

    Empty means NOT RESOLVED YET: the address is selected at deploy time, immediately before
    red01 is created, so a preflight hours earlier cannot prove it."""
    override = red01_ip_override(comp_dir)
    if override:
        return override
    placement = _placement_of(comp_dir) if placement is None else placement
    return ((placement or {}).get("red01") or {}).get("ip") or ""


def red01_ip_legacy(comp_dir):
    """The pre-placement address chain (Compfile, else the one legacy default)."""
    return red01_ip_override(comp_dir) or DEFAULT_RED01_IP


def red01_v1_target(comp_dir, engine_vmid):
    """(vmid, ip) the pre-placement code would have deployed this range's red01 at."""
    return red01_vmid_legacy(comp_dir, engine_vmid), red01_ip_legacy(comp_dir)


def red01_identity(comp_dir, engine_vmid, node, placement=None):
# This run's red01 slot, as the placement document records it.
    return {"vmid": red01_vmid_for(comp_dir, engine_vmid, placement),
            "ip": red01_ip_for(comp_dir, placement), "node": node,
            "explicit_ip": red01_ip_is_explicit(comp_dir)}


def red01_target(comp_dir, engine_vmid, placement=None):
    """(vmid, ip) this competition's red01 deploys at — one resolution for the
    deploy, the scrim harness, the verifier and the preflight, so they cannot
    disagree (they used to: .198 vs .199 vs whatever the singleton said)."""
    return (red01_vmid_for(comp_dir, engine_vmid, placement),
            red01_ip_for(comp_dir, placement))


def pin_deploy_key(cfg, ssh_key):
    """Pin the deploy's ssh_key to the RUN's resolved key.

    bad-auto reads `<key>.pub` beside the key, and its built-in default is a SIBLING
    tezcatlipoca checkout's `proxmox` — a path that does not exist on a run host (the engine
    there is a per-run worktree). Every path that writes a bad-auto config for this range must
    pin the key, or `badauto deploy` dies with FileNotFoundError after the range is built
    (2026-10-14: the assume-breach path, then red staging). No key → leave the config alone.
    """
    if not ssh_key:
        return cfg
    deploy = dict(cfg.get("deploy") or {})
    deploy["ssh_key"] = str(Path(ssh_key).expanduser().resolve())
    cfg["deploy"] = deploy
    return cfg


def prepare_deploy_config(comp_dir, engine_vmid, engine_ip=None, ssh_key=None, placement=None,
                          red_ip=None):
    """Write the comp's bad-auto config for a red01 deploy and return its path.

    Layers: carried blackbox blocks (comp's own prior config) under a fresh
    deploy identity — competition_dir absolute, red_vmid/red_ip from this
    comp's reservation, engine_ip injected so the config is self-sufficient
    (bad-auto's own fallback would need the abs comp dir anyway). The realm
    layer (local-C2 retarget + synthesis) is applied by the caller through
    realm_c2_ops when the Compfile enables it — this function owns the base,
    and the caller owns the layer, same split prepare_local_c2 always had."""
    cfg = dict(existing_blackbox(comp_dir))
    cfg["competition_dir"] = str(Path(comp_dir).resolve())
    vmid, ip = red01_target(comp_dir, engine_vmid, placement)
    # A single-node range has no placement to record the selection in; the caller that made
    # it passes the address through rather than letting it be re-derived here.
    ip = red_ip or ip
    if vmid is None:
        raise ValueError(
            "cannot reserve red01's vmid: no Compfile red01_vmid, no comp config "
            "deploy.red_vmid, and no scoring vmid to derive from")
    if not ip:
        # Never write an empty red_ip: bad-auto falls back to its own default and the VM
        # lands somewhere nobody recorded (the 2026-10-09 .199 split).
        raise ValueError(
            "red01's address is not resolved — select one from the red band (or set "
            "red01_ip in the Compfile) before deploying it")
    deploy = dict(cfg.get("deploy") or {})
    deploy["red_vmid"] = vmid
    deploy["red_ip"] = ip
    if engine_ip and not deploy.get("engine_ip"):
        deploy["engine_ip"] = engine_ip
    cfg["deploy"] = deploy
    # After the assignment: pin_deploy_key reads cfg["deploy"] back, so pinning before this line
    # would be overwritten by the local dict (caught by the peer's key-pinning test).
    pin_deploy_key(cfg, ssh_key)
    return write_config(comp_dir, cfg), vmid, ip
