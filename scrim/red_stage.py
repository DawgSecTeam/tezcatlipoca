import os
from pathlib import Path

from scrim import compworld
from scrim import core
from scrim import endpoints
from scrim import procs
from scrim import red_link
from scrim import red_tunnel
from scrim import test_folder
from scrim.core import log
import badauto_config_ops
import red_plant_ops
import realm_c2_ops


def verify_red_reaches_teams(args, comp, creds):
    """Pre-T0 gate: red01 must be able to dial a box on EVERY team.

    scale8-soak-2026-10-02: routed red01 could not reach any satellite team box — all
    15 cred_sprays and both db_attacks failed "unreachable over SSH" — and the run went
    40 minutes before anyone noticed, because nothing checked red's path before T0 and
    `verify --red-identity` only proved red reached one box on its own segment. Red
    spent the event scouting a network half of which it could not touch.

    Raises (stopping the run before T0) rather than warning: a range where red cannot
    reach half the teams is not an event worth starting. Masq-mode red shares the team
    gateways and has no path of its own to prove, so it is skipped."""
    if (args.red_mode or "routed") == "masq":
        log("--red-mode masq: red shares the team gateways, skipping the reachability gate")
        return
    log("pre-T0: red01 -> every team reachability gate")
    procs.run(compworld.verify_cmd(comp, creds, "--red-ip", args.red_ip,
                                   "--red-identity", "--red-teams", "all"),
              cwd=core.REPO, timeout=900, check=True, tail=25)


def red_pacing_caps():
    """The three concurrent-down caps red is paced by, in ONE place.

    They land in bad-auto's config (stage_red) AND in the run manifest (record_phase) —
    the scrim report judges max_simultaneous_down against the END cap, and a gate that
    disagrees with the pacing it judges is a gate that fails red for obeying orders."""
    return {"max_concurrent_down_start": 2, "max_concurrent_down_end": 4,
            "max_concurrent_down_endgame": 6}


def _existing_blackbox(comp):
    """realm/beacon/evasion blocks from the competition's own bad-auto config,
    if a prior run left one.

    These describe the club's C2 infrastructure (tavern on the malware-dev VM,
    the per-transport imix table, the raw-socket channel), NOT the run's pacing
    — so this stage owns llm/pacing/deploy and must carry them through instead
    of wiping them. Before this, stage_red rewrote config.yaml with no realm
    block at all, which silently dropped every run to the raw-socket beacon
    tier: realm_plant on red01 saw realm.enabled false and the "newly improved"
    multi-transport beacons were never planted on a range.

    Read from the COMP'S config (per-run since the 2026-10-12 isolation), never
    the sibling checkout's repo-root singleton — on a shared checkout that file
    names whichever session staged last.
    """
    return badauto_config_ops.existing_blackbox(comp)


def _resolve_red_vmid(args, comp):
    """Fill a missing --red-vmid from the comp's own reservation: Compfile
    `red01_vmid`, else the comp config's deploy.red_vmid, else derived from the
    deploy state's scoring vmid (engine+170). Refuses when none resolve — the
    old fallback regexed bad-auto's SOURCE for its built-in 999, a shared node
    slot no second session may silently default onto."""
    if getattr(args, "red_vmid", None):
        return args.red_vmid
    engine_vmid = None
    try:
        engine_vmid = core.read_comp_json(comp, ".deploy_state.json").get("scoring_vm_id")
    except Exception:                                        # noqa: BLE001 - best effort
        pass
    vmid = badauto_config_ops.red01_vmid_for(comp, engine_vmid)
    if vmid is None:
        raise RuntimeError(
            "cannot resolve red01's vmid: no --red-vmid, no Compfile red01_vmid, no "
            "comp bad-auto config, and no scoring_vm_id in .deploy_state.json. "
            "Pass --red-vmid or set red01_vmid in the Compfile — never fall back "
            "to bad-auto's shared built-in default (vmid 999).")
    args.red_vmid = vmid
    return vmid


def red_llm_config(args):
    """The `llm` block of red's generated config.

    Local endpoints (llama.cpp/qwen) are slow AND may be reasoning models, so the
    numbers here must clear both costs at once: a small prompt already takes ~60 s
    and the model emits reasoning tokens before any content. At timeout 120 /
    max_tokens 4096 every decision came back ok=false ("Read timed out") or
    finish=length with empty content, so the director silently fell back to
    scripted tactics — red looked busy for a whole event and decided nothing
    (live 2026-10-09). 300 s + 6144 still bounds a single decision and a fast
    model simply answers early; cloud keeps the tighter 240/1 pair.
    """
    local = core.is_local_endpoint(args.llm_base_url)
    llm = {"base_url": args.llm_base_url, "model": args.red_model,
           "max_tokens": 6144 if local else 4096,
           "timeout": 300 if local else 240,
           "json_retries": 0 if local else 1,
           # Bound the DECISION wait, not just the HTTP read: the director's
           # default decide budget is 360 s, and under load (blue cycles on the
           # same endpoint) a slow decision burned 3x that as one silent stall —
           # red's 20-min no-action window in run4 (2026-10-12) is three such
           # waits. 180 s: two re-prompts' worth; past it the deterministic
           # ladder takes the cycle, and the ladder presses instead of idling.
           "decide_budget_sec": 180,
           "first_decision_budget_sec": 90}
    if args.reasoning_effort:
        llm["reasoning_effort"] = args.reasoning_effort
    return llm


def stage_red(args, comp, creds, run_dir):
    llm = red_llm_config(args)
    _resolve_red_vmid(args, comp)   # before the cfg build: the config carries it
    cfg = {
        "llm": llm,
        "intel": "nakon",
        "competition_dir": str(comp.resolve()),
        "event": {"duration_min": args.duration_min},
        "pacing": {"decision_window_min": 2, "window_jitter_min": 1,
                   "active_burst_min": 15, "burst_jitter_min": 2, "quiet_min": 2, "quiet_jitter_min": 1,
                   "focus_rotation_min": max(10, args.duration_min // 8),
                   **red_pacing_caps(),
                   "access_deadline_remaining_min": args.duration_min // 4,
                   "endgame_start_remaining_min": 15,
                   "endgame_decision_window_sec": 60, "endgame_force_active": True,
                   "min_standing_services": 2, "credlist_gate_min": args.duration_min // 4,
                   "credlist_max_per_team": 1, "lockout_gate_pct": 0.75,
                   # Severity ramp + burndown, explicit so the manifest records them:
                   # no takedowns for the first 10 min (blue's discovery window),
                   # recoverable stops only until halfway, then everything unlocks,
                   # and the last 5 min lock every blue login out.
                   "impact_start_min": 10, "unrecoverable_at_pct": 0.5,
                   # The CDE packet's escalation ladder, made harder (brutal tier
                   # combines rename+config-break). Times scale to the event.
                   "escalation": {"enabled": True, "pause_after_restore_min": 5},
                   "burndown_enabled": True, "burndown_remaining_min": 5},
        "limits": {"nmap_timing": "T3", "nmap_top_ports": 200,
                   "max_retries_per_service": 4, "spray_attempts_per_target": 24,
                   "action_timeout": 120, "scan_timeout": 900},
        "deploy": {"red_ip": args.red_ip, "red_gw": args.red_gw, "red_storage": args.red_storage,
                   "red_vmid": args.red_vmid,
                   **({"template": args.red_template} if args.red_template else {}),
                   **({"red_mode": args.red_mode} if args.red_mode else {}),
                   **({"red_subnet": args.red_subnet} if args.red_subnet else {}),
                   **({"red_seg_ip": args.red_seg_ip} if args.red_seg_ip else {})},
    }
    # The operator's C2 blocks survive: stage_red builds llm/pacing/deploy/limits,
    # and `badauto deploy` stages this config onto red01, where realm_plant runs.
    carried = _existing_blackbox(comp)
    for key in ("realm", "beacon", "evasion", "naming"):
        if carried.get(key):
            cfg[key] = carried[key]
    # `realm.c2_local: true` moves the C2 ONTO this run's red01: retarget the
    # carried realm block before the deploy (the engine DNAT bakes c2_ip in at
    # deploy time) and provision tavern on red01 after the clone (below).
    # apply_local_c2 synthesizes the FULL block (transports/callback_ip), and the
    # completeness check refuses pre-T0 rather than stage a dark C2 — the harness
    # path raises where the deploy path degrades.
    if (cfg.get("realm") or {}).get("c2_local"):
        realm_c2_ops.apply_local_c2(cfg["realm"], args.red_ip,
                                    callback_ip=realm_c2_ops.callback_ip_for(comp))
        missing = realm_c2_ops.missing_local_c2_keys(cfg["realm"])
        if missing:
            raise RuntimeError(
                f"realm.c2_local is on but the staged realm block lacks {missing} — "
                "this plants dead beacons under a 'tavern live' banner (2026-10-11). "
                "Fix the comp's realm block or the teams.json and re-run.")
    # ...and so does the REST of the operator's deploy block. stage_red builds this
    # config from scratch, so anything it does not set is lost: `template` most of
    # all, which then fell back to badauto's built-in default and failed the first
    # clean clone of a red01 (the lookup is skipped when the VM already exists, so
    # this only broke every other run — the one that follows a teardown). Keys the
    # harness owns (red_ip/red_gw/red_storage/red_vmid/mode/subnet) still win.
    op_deploy = carried.get("deploy") or {}
    cfg["deploy"] = {**{k: v for k, v in op_deploy.items() if k not in cfg["deploy"]},
                     **cfg["deploy"]}
    # The COMPETITION's own config — one file per deploy, threaded to every
    # badauto call below (and bad-auto stamps .deploy-stamp.json next to it, so
    # concurrent sessions cannot clobber each other's red01 identity — the
    # 2026-10-12 vmid-999 kill). Atomic + 0600: a torn config would break every
    # subsequent badauto call, and the file may name an internal endpoint.
    config_file = badauto_config_ops.write_config(comp, cfg)
    env = {**os.environ, "BAuto_LLM_API_KEY": endpoints.api_key(local=core.is_local_endpoint(args.llm_base_url)),
           "BAuto_STATE_DIR": str(Path(run_dir) / "bad-auto-state")}
    # A stale BAuto_CONFIG in the environment would shadow nothing while --config
    # is explicit, but scrub it anyway: symmetry with red_plant_ops._deploy_red.
    env.pop("BAuto_CONFIG", None)
    procs.run(["python3", "-m", "badauto", "--config", str(config_file), "validate-llm"],
              cwd=core.BAD_AUTO, env=env, timeout=300, tail=3)
    procs.run(["python3", "-m", "badauto", "--config", str(config_file), "run", "--once",
               "--dry-run", "--competition", str(comp.resolve())],
              cwd=core.BAD_AUTO, env=env, timeout=600, tail=6)

    tunnel = red_tunnel.maybe_start_red_tunnel(args)
    if tunnel:
        # The operator-side validate/dry-run above used the real URL on purpose;
        # red01 itself can only dial the endpoint through the tunnel.
        cfg["llm"]["base_url"] = tunnel.red_base_url()
        # Atomic, same reason as above.
        badauto_config_ops.write_config(comp, cfg)
        args.red_tunnel = tunnel
    red_mode = args.red_mode or "routed (bad-auto default)"
    log(f"deploying red01 at {args.red_ip} (vmid {args.red_vmid}, storage "
        f"{args.red_storage}, mode {red_mode})")
    procs.run(["python3", "-m", "badauto", "--config", str(config_file), "deploy",
               "--competition", str(comp.resolve())],
              cwd=core.BAD_AUTO, env=env, timeout=1800)
    # The C2 lives on red01 for this run (operator set realm.c2_local): build
    # tavern there — every callback transport + MCP — and stage the imix
    # implants, BEFORE the seed (realm_plant refuses without a tavern to
    # register against and without staged binaries). Raises: this runs pre-T0.
    if (cfg.get("realm") or {}).get("c2_local"):
        c2 = realm_c2_ops.provision_realm_c2(
            comp, args.red_ip, str(core.REPO / "proxmox"), realm=cfg["realm"],
            repo=cfg["realm"].get("repo", realm_c2_ops.DEFAULT_REPO),
            go_version=cfg["realm"].get("go_version", realm_c2_ops.DEFAULT_GO_VERSION),
            implant_host=cfg["realm"].get("implant_host",
                                          realm_c2_ops.DEFAULT_IMPLANT_HOST))
        if not c2.get("ok"):
            raise RuntimeError(f"realm C2 provisioning on red01 failed: "
                               f"{c2.get('error', 'unknown')} — beacons cannot "
                               f"plant without their tavern")
        log(f"realm C2 live on red01 {args.red_ip} "
            f"(transports: {', '.join(c2['transports'])}; MCP at {c2['mcp']})")
    # Record red01's identity now, while it is known: the manifest is the per-run
    # source of truth the collector dials (INV5), recorded before the LLM gate
    # below so a run that dies there still lets teardown collect from red01.
    # The vmid was resolved up top (_resolve_red_vmid) — the comp's own
    # reservation, never bad-auto's shared built-in default.
    test_folder.record_red_agent(args)

    red_base = cfg["llm"]["base_url"]
    if not red_link.check_red_llm(args, red_base):
        raise RuntimeError(
            f"red01 cannot reach the LLM endpoint ({red_base}) — refusing to start the event "
            f"red-LLM-less. Run a socat relay on this host and/or the reverse tunnel "
            f"(--red-tunnel), then re-run. See docs/scrim-harness.md, 'stage_red (LLM gate)'.")
    log(f"red01 reached the LLM at {red_base} — clear to start")
    # Second pre-T0 gate, same reasoning as the LLM one above: red that cannot dial the
    # teams cannot attack them, and the soak burned 40 minutes finding that out.
    verify_red_reaches_teams(args, comp, creds)
    # Third pre-T0 step: the day-0 seed. CCDC reality is that red is ALREADY inside
    # when the clock starts, so access + the prebaked Realm C2 beacons + the
    # persistence/evasion layer are planted BEFORE T0, not live during the event.
    # Runs last so a seed failure stops the run before scored time (same contract as
    # the two gates above — a scrim with no beacons planted is not the event asked for).
    seed_assume_breach(args, run_dir)
    # The deploy/seed has just written itself into every box's logs — auth.log, the
    # journal, apt history, the shell histories. Wipe them BEFORE blue can log in:
    # an agent that greps those gets the whole plant, names and all, which is the
    # one thing the randomized naming cannot protect against. Best-effort: a wipe
    # that fails is reported, never fatal.
    try:
        from scrim import clean_trails
        clean_trails.clean_trails(comp, creds)
    except Exception as e:                                   # noqa: BLE001 - never fatal
        log(f"WARNING: trail wipe failed ({type(e).__name__}: {e}) — blue may be able "
            f"to read the deploy out of the box logs")
    # Only now start the director: it must load the SEEDED world, not an empty one
    # (and red's event clock should start at T0, which the harness stamps right
    # after stage_red returns).
    red_link.start_red_director(args)


def seed_assume_breach(args, run_dir=None):
    """Plant the assume-breach presence on every team box over red01, pre-T0.

    Delegates to bad-auto's `seed` on red01 (its own transport + intel + guardrails
    — one implementation, no drift from the deploy-time path in `red_plant_ops`).
    Raises on failure: it runs before T0, so aborting costs nothing scored, and
    the whole point of the mode is that the beacons are already planted.
    """
    if not getattr(args, "seed_red", True):
        log("--no-seed-red: skipping the day-0 assume-breach seed (no pre-planted beacons)")
        return
    depth = int(getattr(args, "seed_depth", 3) or 3)
    ssh_key = core.REPO / "proxmox"
    log(f"seeding assume-breach presence on red01 {args.red_ip} (depth {depth}) — "
        f"access + realm beacons + persistence, before T0")
    res = red_plant_ops.seed_via_red01(str(ssh_key), args.red_ip, depth, timeout=5400)
    tail = ((res.stdout or "") + (res.stderr or ""))[-600:]
    for line in (res.stdout or "").splitlines()[-12:]:
        print("   ", line)
    if res.returncode != 0:
        raise RuntimeError(
            f"assume-breach seed failed on red01 (rc={res.returncode}) — refusing to start "
            f"an event with no pre-planted beacons. Fix and re-run with --skip-deploy, "
            f"or pass --no-seed-red to run without the seeded presence.\n{tail}")
    log("assume-breach presence seeded — every box is compromised before T0")
