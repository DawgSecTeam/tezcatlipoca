import json
import os
from pathlib import Path

from config_ops import write_text_atomic
from scrim import compworld
from scrim import core
from scrim import endpoints
from scrim import procs
from scrim import red_link
from scrim import red_tunnel
from scrim import test_folder
from scrim.core import log
import red_plant_ops


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


def stage_red(args, comp, creds, run_dir):
    # Local endpoints (llama.cpp/qwen) are slow: tighter call timeout and no
    # JSON-retry double-call, or one decision can eat 8-16 min of a 90-min event.
    local_llm = core.is_local_endpoint(args.llm_base_url)
    llm = {"base_url": args.llm_base_url, "model": args.red_model,
           "max_tokens": 4096, "timeout": 120 if local_llm else 240,
           "json_retries": 0 if local_llm else 1}
    if args.reasoning_effort:
        llm["reasoning_effort"] = args.reasoning_effort
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
                   **({"red_vmid": args.red_vmid} if args.red_vmid else {}),
                   **({"template": args.red_template} if args.red_template else {}),
                   **({"red_mode": args.red_mode} if args.red_mode else {}),
                   **({"red_subnet": args.red_subnet} if args.red_subnet else {}),
                   **({"red_seg_ip": args.red_seg_ip} if args.red_seg_ip else {})},
    }
    # Atomic: a torn config.yaml would break every subsequent badauto call, and the
    # file may name an internal endpoint.
    write_text_atomic(core.BAD_AUTO / "config.yaml", json.dumps(cfg, indent=2), mode=0o600)
    env = {**os.environ, "BAuto_LLM_API_KEY": endpoints.api_key(local=core.is_local_endpoint(args.llm_base_url)),
           "BAuto_STATE_DIR": str(Path(run_dir) / "bad-auto-state")}
    procs.run(["python3", "-m", "badauto", "validate-llm"], cwd=core.BAD_AUTO, env=env, timeout=300, tail=3)
    procs.run(["python3", "-m", "badauto", "run", "--once", "--dry-run",
               "--competition", str(comp.resolve())], cwd=core.BAD_AUTO, env=env, timeout=600, tail=6)

    tunnel = red_tunnel.maybe_start_red_tunnel(args)
    if tunnel:
        # The operator-side validate/dry-run above used the real URL on purpose;
        # red01 itself can only dial the endpoint through the tunnel.
        cfg["llm"]["base_url"] = tunnel.red_base_url()
        # Atomic, same reason as above.
        write_text_atomic(core.BAD_AUTO / "config.yaml", json.dumps(cfg, indent=2), mode=0o600)
        args.red_tunnel = tunnel
    red_mode = args.red_mode or "routed (bad-auto default)"
    log(f"deploying red01 at {args.red_ip} (storage {args.red_storage}, mode {red_mode})")
    procs.run(["python3", "-m", "badauto", "deploy", "--competition", str(comp.resolve()), "--start"],
              cwd=core.BAD_AUTO, env=env, timeout=1800)
    # Record red01's identity now, while it is known: bad-auto's config.yaml is a rewritten
    # singleton, so a later reader of it can name ANOTHER run's red01 — the manifest is the
    # only per-run source of truth the collector may dial (INV5). Recorded before the LLM
    # gate below so a run that dies there still lets teardown collect from red01.
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
