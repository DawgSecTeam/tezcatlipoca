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

from common.utils import compfile_value

# The one file name for a competition's bad-auto config. YAML on disk (JSON
# parses as YAML, so hand-written JSON in it also loads).
CONFIG_NAME = ".badauto-config.yaml"

# red01's default address — ONE default for every path (the .198/.199 split
# between the scrim CLI and the deploy path meant two sessions could each be
# "right" about different addresses).
DEFAULT_RED01_IP = "10.0.0.198"

# red01's derived vmid offset from the scoring engine's. The engine's reserved
# block: +140 engine template, +150+slot*N goldens, +130+slot jumps (multi-node)
# — +170 sits clear of all of them, so a scoring vmid that is collision-free
# yields a red01 vmid that is too (the preflight gate still verifies).
RED01_VMID_OFFSET = 170


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


def red01_vmid_for(comp_dir, engine_vmid=None):
    """red01's vmid: Compfile `red01_vmid <n>`, else the comp config's
    deploy.red_vmid, else derived engine+170 (None when nothing resolves).

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
    if engine_vmid is None:
        return None
    return int(engine_vmid) + RED01_VMID_OFFSET


def red01_ip_for(comp_dir):
    """red01's address: Compfile `red01_ip` (legacy `assume_breach_red_ip`
    still honored), else the one tez default."""
    compfile = Path(comp_dir) / "Compfile"
    return (compfile_value(compfile, "red01_ip", "")
            or compfile_value(compfile, "assume_breach_red_ip", "")
            or DEFAULT_RED01_IP)


def red01_target(comp_dir, engine_vmid):
    """(vmid, ip) this competition's red01 deploys at — one resolution for the
    deploy, the scrim harness, the verifier and the preflight, so they cannot
    disagree (they used to: .198 vs .199 vs whatever the singleton said)."""
    return red01_vmid_for(comp_dir, engine_vmid), red01_ip_for(comp_dir)


def prepare_deploy_config(comp_dir, engine_vmid, engine_ip=None):
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
    vmid, ip = red01_target(comp_dir, engine_vmid)
    if vmid is None:
        raise ValueError(
            "cannot reserve red01's vmid: no Compfile red01_vmid, no comp config "
            "deploy.red_vmid, and no scoring vmid to derive from")
    deploy = dict(cfg.get("deploy") or {})
    deploy["red_vmid"] = vmid
    deploy["red_ip"] = ip
    if engine_ip and not deploy.get("engine_ip"):
        deploy["engine_ip"] = engine_ip
    cfg["deploy"] = deploy
    return write_config(comp_dir, cfg), vmid, ip
