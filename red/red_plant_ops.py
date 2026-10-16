"""Plant the assume-breach red presence on the team boxes during setup.

CCDC reality: red is ALREADY inside when the clock starts. The prebaked implant
(Realm C2 imix — one independent beacon per transport) and the standing
persistence/evasion layer must be on the boxes BEFORE T0, not planted live during
the event. tezcatlipoca's setup is where that happens: this step runs in phase 7,
after the nakon final pass and BEFORE the `tz-ready` snapshot, so the restore
point every box carries is already-compromised and phase 8 starts the clock on a
range red owns.

It delegates to bad-auto (sibling checkout) rather than re-implementing the
plant, so exactly one copy of the planting logic exists:

    badauto deploy --competition <dir>        # red01 + the realm engine DNAT
    ssh red01  sudo python3 -m badauto seed   # access + beacons + persistence

Gated by the Compfile knob ``assume_breach`` (default 0, so existing Compfiles are
unchanged). ``assume_breach_depth`` (default 3) picks the seed depth, and
``assume_breach_red_ip`` overrides red01's address.

The Realm C2 the seed plants against lives ON red01 by default (``realm_c2_local``
1; ``0`` keeps the legacy tavern-on-VM101 layout). ``realm_c2_ops`` derives
bad-auto's config before the deploy — the engine DNAT bakes ``realm.c2_ip`` in at
deploy time — then builds tavern from source on red01 with every callback
transport (grpc + http1/dns/quic/icmp redirectors) and the MCP server, relaying
the prebuilt imix implants from ``realm_c2_implant_host`` (VM101) into bad-auto's
staging paths. ``realm_c2_repo`` / ``realm_c2_go_version`` / ``realm_c2_implant_host``
override the upstream URL, the Go toolchain and the implant host.

Failure degrades: it records a degradation and continues — a planting problem must
not abort a deploy whose range is otherwise healthy. verify-competition and the
red report catch it before T0.

TEARDOWN NOTE: red01 is bad-auto's VM, not a tezcatlipoca box. destroy-competition
does NOT remove it (its run-ownership tags never covered it) — run
``python3 -m badauto destroy --competition <dir> --yes`` as part of teardown.
This step records red01's identity in ``state["assume_breach"]`` so teardown and
the report can name it.
"""

import os
import subprocess
from pathlib import Path

from common.constants import red_bridge_name
from common.utils import compfile_flag, compfile_value, local_python, record_degradation
from red import badauto_config_ops
from red import realm_c2_ops

REPO = Path(__file__).resolve().parents[1]
BAD_AUTO = REPO.parent / "bad-auto"

RED_USER = "sysadmin"
SEED_DEPTH_MAX = 3
# Where `badauto deploy` stages the package on red01 (the systemd unit's
# WorkingDirectory — the package is imported by cwd, never pip-installed).
INSTALL_DIR = "/opt/bad-auto"


def _bad_auto_present():
    return (BAD_AUTO / "badauto" / "__main__.py").exists()


def _configured_red_ip(comp_dir):
    """red01's address from the competition's own bad-auto config, when one exists."""
    cfg = badauto_config_ops.load_config(comp_dir)
    return ((cfg.get("deploy") or {}).get("red_ip")) or None


def _configured_red_vmid(comp_dir):
    """red01's vmid from the competition's own bad-auto config (deploy.red_vmid —
    what `badauto destroy` actually acts on)."""
    cfg = badauto_config_ops.load_config(comp_dir)
    return ((cfg.get("deploy") or {}).get("red_vmid")) or None


def red_vmid_candidates(comp_dir, deployed_state=None):
    """Every place red01's vmid is recorded, most-authoritative first: the comp's
    bad-auto config (deploy.red_vmid — what `badauto destroy` actually acts on),
    then the deploy state's assume-breach record. Teardown's verify step uses
    this to prove the specific VM is gone (2026-10-08: an unknown vmid made the
    check unverifiable and the leak invisible)."""
    candidates = [_configured_red_vmid(comp_dir)]
    recorded = ((deployed_state or {}).get("assume_breach") or {}).get("vmid")
    if recorded:
        candidates.append(recorded)
    return [c for c in candidates if c]


def parse_teardown_refusal(proc):
    """The bad-auto contract's machine line (exit 17): TEARDOWN-REFUSAL
    {"saw","expected","remedy"} on stdout — the shared refusal vocabulary,
    parsed so the teardown renders bad-auto's refusals like its own."""
    import json
    for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
        if line.startswith("TEARDOWN-REFUSAL "):
            try:
                return json.loads(line.split(" ", 1)[1])
            except ValueError:
                return None
    return None


def red_ip_for(comp_dir, placement=None):
# The address red01 will be created at: the operator's Compfile value, the placement's
#     recorded one, else '' — the selection happens at plant time (_resolve_red_ip).
    return badauto_config_ops.red01_ip_for(comp_dir, placement)


def select_red_ip_for(comp_dir, placement, engine_mgmt_ip="", comp_tags=None):
# Select an address from the red band NOW and record it in the placement.
#
#     Called immediately before red01 is created (deploy phase 7, or a scrim staging red on
#     a range that never deployed one): a selection made in prepare() is hours early, and two
#     ranges submitted together would both take the same "lowest free" address.
    from estate import config
    from infra import collisions
    from multinode.placement_record import write_red01_ip
    claimed = {engine_mgmt_ip or "", config.active().engine.mgmt_gw or ""}
    nodes = [config.active().proxmox.node]
    skip = set()
    if placement:
        claimed |= {s["jump_mgmt_ip"] for s in placement.get("satellites") or []}
        nodes = [rec["node"] for rec in placement["nodes"].values()]
        skip |= {s["jump_vmid"] for s in placement.get("satellites") or []}
        skip.add(placement.get("engine_vmid") or 0)
    ip, unchecked = collisions.select_red_ip(
        collisions.scan_vms_for(nodes), claimed, skip, comp_tags)
    if ip and placement:
        write_red01_ip(comp_dir, placement, ip)
    return ip, unchecked


def _resolve_red_ip(ctx):
# red01's address for this deploy: the operator's value, the placement's recorded one, else
#     selected now. The residual window (a VM created between the scan and bad-auto's clone)
#     is real and is why the recorded value is never re-derived.
    override = badauto_config_ops.red01_ip_override(ctx.comp_dir)
    if override:
        return override, []
    placement = getattr(ctx, "placement", None)
    recorded = ((placement or {}).get("red01") or {}).get("ip")
    if recorded:
        return recorded, []
    return select_red_ip_for(ctx.comp_dir, placement,
                             engine_mgmt_ip=getattr(ctx, "engine_mgmt_ip", "") or "",
                             comp_tags=getattr(ctx, "comp_tags", None))


def _deploy_red(comp_dir, env, config_path=None, timeout=1800):
    """`badauto deploy` (no --start): red01 + the realm engine DNAT, no director.

    `config_path` (realm_c2_ops's derived config) makes the deploy point the
    engine DNAT at red01 itself instead of the static C2 host.

    The competition path is ABSOLUTE on purpose: this runs with cwd=BAD_AUTO, and
    `badauto deploy` resolves `<comp_dir>/credentials.txt` (its engine-IP source) —
    a relative path would look there instead of in the competition directory and
    die with "engine IP unknown" (caught live 2026-10-08).
    """
    comp_abs = Path(comp_dir).resolve()
    cmd = [local_python(), "-m", "badauto"]
    if config_path:
        # Resolve: badauto runs with cwd=BAD_AUTO, so a relative --config resolves
        # inside ITS tree. And never hand it a path that isn't there: load_config
        # then falls back to the built-in defaults WITHOUT a word, which is how a
        # 95-minute deploy built red01 at the default red_ip 10.0.0.199 while every
        # other component targeted the comp's 10.0.0.198 and the whole red presence
        # died with "No route to host" (2026-10-09).
        cfg_path = Path(config_path).resolve()
        if not cfg_path.exists():
            return subprocess.CompletedProcess(
                cmd, 2, "",
                f"derived config not found: {cfg_path} — refusing to deploy red01 on "
                f"badauto's built-in defaults (red01 would come up at the wrong address)")
        cmd += ["--config", str(cfg_path)]
        # An explicit --config makes any inherited BAuto_CONFIG redundant; dropping it
        # keeps a stale value in the deploy's environment from shadowing ours.
        env = {k: v for k, v in (env or {}).items() if k != "BAuto_CONFIG"}
    cmd += ["deploy", "--competition", str(comp_abs)]
    return subprocess.run(cmd, cwd=str(BAD_AUTO), env=env,
                          capture_output=True, text=True, timeout=timeout)


def destroy_red(comp_dir, env=None, timeout=900):
    """`badauto destroy`: red01 + its realm NAT rules.

    red01 is bad-auto's VM, not an estate-managed box, so destroying the range
    leaves it running — with its LLM key, its beacon tasking and its DNAT rules.
    That is the scale8 soak leak (red01 998 alive after the driver printed DONE).
    The harness destroys it in its own teardown; this is the step the standalone
    teardown was missing for the run whose harness died."""
    # BAuto_COMPETITION_DIR must be set: bad-auto's destroy verifies the VM's deploy
    # stamp against the competition directory, and without it the identity guard
    # refuses the target ("mismatched target is refused") — caught live 2026-10-08,
    # where destroy-competition's env reached the call without it.
    #
    # Absolute for the same reason _deploy_red resolves: this runs with cwd=BAD_AUTO
    # and badauto discovers the engine IP from <comp_dir>/credentials.txt, so a
    # relative path resolves under bad-auto, discovery fails and the guard refuses
    # with "engine IP unknown" — caught live 2026-10-09, with the range already gone
    # and red01 the only thing left standing.
    comp_dir = Path(comp_dir).resolve()
    env = {**(env if env is not None else os.environ), "BAuto_COMPETITION_DIR": str(comp_dir)}
    cmd = [local_python(), "-m", "badauto"]
    # The comp's own config (with its own deploy stamp next to it) when there is
    # one — never a missing path: load_config falls back to built-in DEFAULTS
    # (red_vmid 999) without a word, and destroy would aim at the wrong target.
    cfg_path = badauto_config_ops.config_path(comp_dir)
    if cfg_path.exists():
        cmd += ["--config", str(cfg_path)]
        # Symmetric with _deploy_red: an explicit --config makes an inherited
        # BAuto_CONFIG redundant; dropping it keeps a stale value from shadowing.
        env = {k: v for k, v in env.items() if k != "BAuto_CONFIG"}
    cmd += ["destroy", "--competition", str(comp_dir), "--yes"]
    return subprocess.run(cmd, cwd=str(BAD_AUTO), env=env,
                          capture_output=True, text=True, timeout=timeout)


def _record_attempt(ctx, red_ip, reason, vmid=None):
    """Record a FAILED assume-breach attempt in the deploy state too. A failed
    deploy may still have left red01 standing (live-found 2026-10-12: the V1
    validation deploy failed after `badauto deploy` created the VM, and the
    teardown exited 0 with red01 up because nothing recorded the attempt).
    verify_clean keys on this record to demand red01's destruction."""
    try:
        ctx.state["assume_breach"] = {"ok": False, "red_ip": red_ip, "vmid": vmid,
                                      "reason": reason, "at": _now()}
        ctx.save_state()
    except Exception:
        pass  # state bookkeeping must never fail the deploy


def _seed_red(ssh_key, red_ip, depth, timeout=3600):
    """Run the day-0 seed on red01 — access + implants + persistence + evasion.

    `cd` into the install dir first: badauto is not pip-installed on red01, it is
    imported by cwd (the systemd unit sets WorkingDirectory=/opt/bad-auto), so
    `sudo -n python3 -m badauto` from the login dir dies with "No module named
    badauto" and the whole pre-T0 seed is lost.
    """
    remote = (f"cd {INSTALL_DIR} && sudo -n python3 -m badauto seed"
              f" --competition /var/lib/bad-auto/intel --state-dir /var/lib/bad-auto"
              f" --intel nakon --depth {depth}")
    return subprocess.run(
        ["ssh", "-i", str(ssh_key),
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "ConnectTimeout=10", f"{RED_USER}@{red_ip}", remote],
        capture_output=True, text=True, timeout=timeout)


def seed_via_red01(ssh_key, red_ip, depth, timeout=3600):
    """Public entry for callers that already own red01 (the scrim harness's
    stage_red does): just run the day-0 seed on it."""
    return _seed_red(ssh_key, red_ip, depth, timeout=timeout)


def plant_assume_breach(ctx):
    """[7/8] Deploy red01 and seed the assume-breach presence. Warn-and-continue.

    Returns a small summary dict; the caller prints/propagates it.
    """
    comp_dir = ctx.comp_dir
    if not _bad_auto_present():
        record_degradation("assume_breach", f"no bad-auto checkout at {BAD_AUTO}")
        print(f"  assume-breach: bad-auto not found at {BAD_AUTO} — skipped")
        return {"ok": False, "reason": "no bad-auto checkout"}

    depth = min(max(compfile_flag(comp_dir / "Compfile", "assume_breach_depth", 3), 0),
                SEED_DEPTH_MAX)
    red_ip, unchecked = _resolve_red_ip(ctx)
    if not red_ip:
        from infra import collisions
        detail = (f" ({len(unchecked)} running guest(s) have no agent to ask: "
                  f"{', '.join(unchecked[:5])})" if unchecked else "")
        record_degradation("assume_breach_red_ip", "no free address in the red band")
        print(f"  assume-breach: REFUSING to create red01 — no address in the red band "
              f"{collisions.RED01_IP_BAND[0]}..{collisions.RED01_IP_BAND[1]} is provably free"
              f"{detail}. Set red01_ip in the Compfile to an address you have checked yourself.")
        return {"ok": False, "reason": "no free red01 address"}
    if unchecked:
        print(f"  assume-breach: red01 takes the lowest unanswered red-band address; "
              f"{len(unchecked)} running guest(s) have no agent to ask")
    ssh_key = getattr(ctx, "ssh_key_abs", None) or getattr(ctx, "ssh_key", None)
    if not ssh_key:
        record_degradation("assume_breach", "no ssh key on the deploy context")
        print("  assume-breach: no ssh key available — skipped")
        return {"ok": False, "reason": "no ssh key"}

    env = {**os.environ, "BAuto_COMPETITION_DIR": str(comp_dir)}
    # The competition's OWN bad-auto config — written before anything else, so
    # every badauto call in this deploy (and its destroy, and its stamp) points
    # at THIS deploy's red01. The base carries the reserved vmid/IP; the realm
    # layer below retargets the C2 the same way it always did.
    engine_vmid = getattr(ctx, "engine_vmid", None)
    if not engine_vmid:
        engine_vmid = (ctx.state or {}).get("scoring_vm_id")
    try:
        base_path, red_vmid, red_ip = badauto_config_ops.prepare_deploy_config(
            comp_dir, engine_vmid, engine_ip=realm_c2_ops.engine_ip_for(comp_dir),
            ssh_key=ssh_key, placement=ctx.placement, red_ip=red_ip,
            red_bridge=red_bridge_name(ctx.run_id))
        config_path = base_path
    except (TypeError, ValueError) as e:
        record_degradation("assume_breach_config", f"cannot reserve red01 identity: {e}")
        print(f"  assume-breach: could not reserve red01's vmid/IP ({e}) — skipped")
        return {"ok": False, "reason": f"red01 reservation failed: {e}"}

    # Realm C2 on red01 (default): the engine DNAT must bake in the C2 address
    # at DEPLOY time, so the derived config is written first. `realm_c2_local 0`
    # keeps the legacy layout (tavern on the static implant-host VM).
    c2_result = None
    realm_cfg = None
    if realm_c2_ops.compfile_knobs(comp_dir)["enabled"]:
        try:
            c2_cfg_path, realm_cfg = realm_c2_ops.prepare_local_c2(
                comp_dir, red_ip, red_vmid=red_vmid)
            if c2_cfg_path is not None:
                config_path = c2_cfg_path
        except ValueError as e:
            record_degradation("realm_c2_config", str(e))
            print("  " + "!" * 70)
            print(f"  realm-c2: REFUSING to deploy the local C2: {e}")
            print("  realm-c2: the 2026-10-11 deploy ran exactly this state — it printed")
            print("  realm-c2: 'tavern live … all transports', seeded implant flags, and")
            print("  realm-c2: the C2 was DARK. Fix teams.json / the bad-auto realm")
            print("  realm-c2: block, then re-run. No red01 was deployed for this.")
            print("  " + "!" * 70)
            return {"ok": False, "reason": "derived realm block missing transports/callback_ip"}
        missing = (realm_c2_ops.missing_local_c2_keys(realm_cfg)
                   if realm_cfg is not None else [])
        if missing:
            record_degradation("realm_c2_config", f"derived block missing {missing}")
            print(f"  realm-c2: WARNING — derived realm block lacks {missing}; "
                  f"beacons will not phone home (see docs/known-issues.md)")
            return {"ok": False, "reason": f"realm block missing {missing}"}
    print("  assume-breach: deploying red01 + realm engine DNAT (bad-auto)...")
    try:
        deployed = _deploy_red(comp_dir, env, config_path=config_path)
    except subprocess.TimeoutExpired:
        record_degradation("assume_breach_deploy", "badauto deploy timed out")
        print("  assume-breach: bad-auto deploy timed out — skipped")
        return {"ok": False, "reason": "deploy timeout"}
    if deployed.returncode != 0:
        tail = ((deployed.stderr or "") + (deployed.stdout or ""))[-300:]
        record_degradation("assume_breach_deploy", tail)
        print("  assume-breach: bad-auto deploy FAILED (see degradations)")
        _record_attempt(ctx, red_ip, "deploy failed", vmid=red_vmid)
        return {"ok": False, "reason": "deploy failed"}

    # The C2 lives on red01 now: build tavern from source there (all callback
    # transports + the MCP server) and stage the prebuilt imix implants into
    # bad-auto's paths. Warn-and-continue — a dead tavern surfaces as plants
    # that refuse and a degradation, not a lost range.
    if realm_cfg is not None:
        knobs = realm_c2_ops.compfile_knobs(comp_dir)
        print(f"  assume-breach: provisioning the Realm C2 (tavern) on red01 "
              f"{red_ip} — all transports + MCP (first run builds from source, "
              f"can take ~10 min)...")
        try:
            c2_result = realm_c2_ops.provision_realm_c2(
                comp_dir, red_ip, ssh_key, realm=realm_cfg,
                repo=knobs["repo"], go_version=knobs["go_version"],
                implant_host=knobs["implant_host"])
        except subprocess.TimeoutExpired:
            c2_result = {"ok": False, "red_ip": red_ip,
                         "error": "realm C2 provisioning timed out"}
        if c2_result.get("ok"):
            print(f"  realm-c2: tavern live on red01 {red_ip} "
                  f"(transports: {', '.join(c2_result['transports'])}; "
                  f"MCP at {c2_result['mcp']})")
        else:
            record_degradation("realm_c2_provision", c2_result.get("error", ""))
            print(f"  realm-c2: WARNING — tavern NOT live on red01 "
                  f"({c2_result.get('error', 'unknown')}); beacons will refuse "
                  f"to plant until it is up")

    print(f"  assume-breach: seeding access + beacons + persistence "
          f"(depth {depth}) on red01 {red_ip}...")
    try:
        seeded = _seed_red(ssh_key, red_ip, depth)
    except subprocess.TimeoutExpired:
        record_degradation("assume_breach_seed", "badauto seed timed out on red01")
        print("  assume-breach: seed timed out — the range is NOT fully seeded")
        return {"ok": False, "reason": "seed timeout"}
    if seeded.returncode != 0:
        tail = ((seeded.stderr or "") + (seeded.stdout or ""))[-300:]
        record_degradation("assume_breach_seed", tail)
        print("  assume-breach: seed FAILED (see degradations)")
        _record_attempt(ctx, red_ip, "seed failed", vmid=red_vmid)
        return {"ok": False, "reason": "seed failed"}

    summary = {"ok": True, "red_ip": red_ip, "vmid": red_vmid,
               "depth": depth, "at": _now()}
    if c2_result is not None:
        summary["realm_c2"] = c2_result
    try:
        ctx.state["assume_breach"] = summary
        ctx.save_state()
    except Exception:
        pass  # state bookkeeping must never fail the deploy
    print(f"  assume-breach: seeded (depth {depth}) — every box is owned before T0. "
          f"Teardown: `python3 -m badauto destroy --competition {comp_dir} --yes`.")
    return summary


def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")
