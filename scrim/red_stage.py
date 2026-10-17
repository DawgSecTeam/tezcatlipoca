"""Stage the red team for a scrim: one deploy path, shared with phase 7.

The harness never provisions red itself: it calls the same
`tlaloc_ops.plant_assume_breach` the deploy pipeline does, then adds the two
pre-T0 gates and the scrim-only LLM wiring.
"""

import json
from pathlib import Path

from common.utils import competition_name
from scrim import core
from scrim import endpoints
from scrim import fire_test
from scrim import procs
from scrim import red_link
from scrim import red_tunnel
from scrim import test_folder
from scrim.core import log
from multinode.placement_record import read_placement
from red import red01_ops


def verify_red_reaches_teams(args, comp, creds):
    """Pre-T0 gate: red01 must be able to dial a box on EVERY team.

    Judged from red's own summary lines, not the verify exit code: the full run's
    rc also carries unrelated estate gates, and a scrim once died at this gate
    with red_identity/red_teams both PASS because pre-existing FAILs rode the
    same rc=1.
    """
    log("pre-T0: red01 -> every team reachability gate [authoritative verify]")
    from scrim import compworld
    r = procs.run(compworld.verify_cmd(comp, creds, "--red-ip", args.red_ip,
                                       "--red-identity", "--red-teams", "all"),
                  cwd=core.REPO, timeout=900, check=False, tail=25)
    fire_test.write_verify_log(args, "pre-T0 (authoritative)", r)
    summary = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if parts and parts[0] in ("red_identity", "red_teams"):
            summary[parts[0]] = line
    failed = [k for k in ("red_identity", "red_teams")
              if "PASS" not in summary.get(k, "")]
    if failed:
        raise RuntimeError(
            f"red reachability gate failed: {', '.join(failed)} did not PASS "
            f"(verify rc={r.returncode}); red01 cannot reach every team — not an "
            f"event to start. stderr: {(r.stderr or '')[-400:]}")


def red_pacing_caps():
    """The three concurrent-down caps red is paced by, in ONE place.

    They land in the run manifest (record_phase) so the scrim report judges
    max_simultaneous_down against the same end cap the agent paces itself by.
    """
    return {"max_concurrent_down_start": 2, "max_concurrent_down_end": 4,
            "max_concurrent_down_endgame": 6}


def red_llm_config(args, base_url=None):
    """The `llm` block of the pushed config.

    A local endpoint (llama.cpp/qwen) is slow and may emit reasoning tokens, so it
    gets a longer read timeout; a fast cloud model simply answers early.
    """
    base = base_url or args.llm_base_url
    local = core.is_local_endpoint(base)
    return {"enabled": True, "base_url": base, "model": args.red_model,
            "timeout_sec": 300 if local else 240,
            "api_key_env": "TLALOC_LLM_API_KEY"}


def _resolve_red_vmid(args, comp):
    """Fill a missing --red-vmid from the comp's own reservation."""
    if getattr(args, "red_vmid", None):
        return args.red_vmid
    placement = read_placement(comp)
    engine_vmid = None
    try:
        engine_vmid = core.read_state(comp).get("scoring_vm_id")
    except Exception:                                        # noqa: BLE001 - best effort
        pass
    vmid = red01_ops.red01_vmid_for(comp, engine_vmid, placement)
    if vmid is None:
        raise RuntimeError(
            "cannot resolve red01's vmid: no --red-vmid, no Compfile red01_vmid, no "
            "recorded placement, and no scoring_vm_id in the state document. Pass "
            "--red-vmid or set red01_vmid in the Compfile.")
    args.red_vmid = vmid
    return vmid


def _resolve_red_ip(args, comp, placement=None):
    """Fill a missing --red-ip from the range's own record, else select one now."""
    if getattr(args, "red_ip", None):
        return args.red_ip
    placement = placement if placement is not None else read_placement(comp)
    ip = red01_ops.red01_ip_for(comp, placement)
    if not ip:
        ip, _unchecked = red01_ops.select_red_ip(comp, placement)
    if not ip:
        raise RuntimeError(
            "no provably free address in the red band — pass --red-ip with an address you "
            "have checked yourself")
    args.red_ip = ip
    return ip


class _PipelineCtx:
    """The little `ctx` phase 7's planter expects, built from a scrim's state."""

    def __init__(self, comp, args, ssh_key):
        from estate import config as estate_config
        self.comp_dir = Path(comp)
        self.comp_id = getattr(args, "comp_id", None) or self.comp_dir.name
        self.ssh_key = str(ssh_key)
        self.ssh_key_abs = str(ssh_key)
        flat = core.read_state(self.comp_dir) or {}
        self.comp_name = competition_name(self.comp_dir)
        self.run_id = flat.get("run_id") or ""
        self.engine_vmid = flat.get("scoring_vm_id")
        self.engine_ip = flat.get("engine_mgmt_ip") or ""
        self.scoring_ip = self.engine_ip
        self.scoring_user = estate_config.active().engine.username
        self.node = estate_config.active().proxmox.node
        self.placement = read_placement(self.comp_dir)
        self.teams = self._teams()
        self.red_ip = getattr(args, "red_ip", None)
        self.state = {}

    def _teams(self):
        try:
            return json.loads((self.comp_dir / "teams.json").read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self):
        pass


def stage_red(args, comp, creds, run_dir):
    """Deploy red01 + its C2 + the day-0 seed, then prove red can act."""
    from red import tlaloc_ops
    _resolve_red_vmid(args, comp)
    # The placement is authoritative once written; passing it avoids re-reading and re-selecting.
    _resolve_red_ip(args, comp, read_placement(comp))
    base_url = args.llm_base_url
    tunnel = red_tunnel.maybe_start_red_tunnel(args)
    if tunnel:
        base_url = tunnel.red_base_url()
        args.red_tunnel = tunnel
    ssh_key = (creds or {}).get("KEY_PATH") or core.REPO / "proxmox"
    ctx = _PipelineCtx(comp, args, ssh_key)
    log(f"staging tlaloc on red01 {args.red_ip} (vmid {args.red_vmid}) — "
        f"Mythic via Gaia, payload freeze, day-0 seed")
    tlaloc_ops.plant_assume_breach(
        ctx, ssh_key=str(ssh_key), llm=red_llm_config(args, base_url),
        llm_key=endpoints.api_key(args.llm_base_url))
    test_folder.record_red_agent(args)
    verify_tlaloc_seeded(args, comp)
    if not red_link.check_red_llm(args, base_url):
        raise RuntimeError(
            f"red01 cannot reach the LLM endpoint ({base_url}) — refusing to start the "
            f"event red-LLM-less. Run a socat relay on this host and/or the reverse "
            f"tunnel (--red-tunnel), then re-run.")
    log(f"red01 reached the LLM at {base_url} — clear to start")
    verify_red_reaches_teams(args, comp, creds)
    try:
        from scrim import clean_trails
        clean_trails.clean_trails(comp, creds)
    except Exception as e:                                   # noqa: BLE001 - never fatal
        log(f"WARNING: trail wipe failed ({type(e).__name__}: {e}) — blue may be able "
            f"to read the deploy out of the box logs")
    red_link.start_red_director(args)


def verify_tlaloc_seeded(args, comp):
    """Pre-T0 gate: the seed's manifest must show a stage landed on at least one box.

    The manifest is pulled by the deploy into `<comp>/red/manifest.json`; nothing
    else proves the range is already owned before T0.
    """
    manifest = Path(comp) / "red" / "manifest.json"
    try:
        data = json.loads(manifest.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"tlaloc seed left no readable manifest at {manifest} ({exc}) — the "
            f"assume-breach presence cannot be proven, not an event to start.")
    boxes = data.get("boxes") or {}
    seeded = [ip for ip, rec in boxes.items() if rec.get("stages")]
    if not seeded:
        raise RuntimeError(
            f"tlaloc seeded nothing: the manifest lists {len(boxes)} box(es) and no "
            f"stage landed on any of them (see {manifest}).")
    log(f"tlaloc seeded {len(seeded)}/{len(boxes)} box(es) — presence planted before T0")
    return seeded
