"""tlaloc CLI — one entry point for the operator, the engine and the seed stage.
"""

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from . import report as report_mod
from . import run as run_mod
from . import seed as seed_mod
from .errors import ConfigError, TlalocError

# Fields the deploy pushes to /etc/tlaloc/config.yaml; an unlisted DEFAULTS leaf is
# dead on the VM while local tests stay green. The engine and tests/test_config_contract.py key off this list.
CONFIG_CONTRACT = (
    "c2.base_url",
    "c2.callback_host",
    "c2.callback_wait_sec",
    "c2.callback_max_age_sec",
    "c2.callback_port",
    "c2.dns_domain",
    "c2.enabled",
    "c2.engine",
    "c2.implant_spread",
    "c2.operation",
    "c2.operator_config",
    "c2.password_env",
    "c2.task_timeout_sec",
    "c2.transports",
    "c2.username",
    "c2.verify_tls",
    "evasion.preload_so",
    "clef.base_url",
    "clef.enabled",
    "clef.high_stakes_effects",
    "clef.max_request_tokens",
    "clef.model",
    "clef.path",
    "clef.theta",
    "clef.theta_by_question",
    "clef.timeout_sec",
    "clef.tokens_per_char",
    "effects.allow_ssh_takedown",
    "effects.decoy",
    "effects.mysql_udf",
    "effects.rekill_cron",
    "effects.rotate_credlist",
    "limits.action_timeout",
    "limits.command_max_len",
    "limits.cred_spray_max",
    "limits.max_retries_per_service",
    "limits.nmap_timing",
    "limits.nmap_top_ports",
    "limits.scan_timeout",
    "limits.spray_delay_sec",
    "limits.spray_probe_timeout",
    "limits.unreachable_halt_after",
    "llm.api_key_env",
    "llm.base_url",
    "llm.enabled",
    "llm.model",
    "llm.timeout_sec",
    "naming.mode",
    "procfilter.install_dir",
    "red.assume_breach",
    "red.budget.extra_pressure_max",
    "red.budget.improvise_commands",
    "red.budget.improvise_rounds",
    "red.caps.grace_min",
    "red.caps.max_down",
    "red.caps.min_standing",
    "red.depth",
    "red.duration_min",
    "red.ladder.enabled",
    "red.ladder.pause_after_restore_min",
    "red.ladder.tiers",
    "red.windows.endgame_teardown",
    "red.windows.lockout",
    "run.blue_ips",
    "run.comp",
    "run.comp_dir",
    "run.decision_window_min",
    "run.dry_run",
    "run.endgame_cycle_min",
    "run.error_halt_after",
    "run.focus_rotation_min",
    "run.host_guard",
    "run.jitter_pct",
    "run.kill_file",
    "run.state_dir",
    "sensors.blue_hunting_every_n_cycles",
    "sensors.ease_silence_min",
    "sensors.poll_timeout_sec",
    "sensors.quotient_enabled",
    "sensors.quotient_url",
    "sensors.tempo_ease_window_min",
    "sensors.tempo_press_window_min",
)


def _dotted(cfg, path):
    node = cfg
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def verify_config(cfg, *, pushed=None):
    problems = []
    for field in CONFIG_CONTRACT:
        local = _dotted(cfg, field)
        if local is None:
            problems.append({"field": field, "problem": "missing locally"})
            continue
        if pushed is None:
            continue
        remote = _dotted(pushed, field)
        if remote is None:
            # Not pushed => the VM uses the default. Compare against that.
            remote = _dotted(config_mod.DEFAULTS, field)
        if remote is None:
            problems.append({"field": field, "problem": "missing from the pushed config"})
        elif json.dumps(remote, sort_keys=True, default=str) != \
                json.dumps(local, sort_keys=True, default=str):
            problems.append({"field": field,
                             "problem": f"pushed {remote!r} != effective {local!r}"})
    return problems


def _load(args):
    cfg = config_mod.load_config(getattr(args, "config", None))
    comp_dir = getattr(args, "comp", None)
    if comp_dir:
        comp_dir = str(Path(comp_dir).resolve())
        cfg["run"]["comp_dir"] = comp_dir
        if not Path(comp_dir).is_dir():
            raise ConfigError(f"--comp {comp_dir} is not a directory")
        cfg = config_mod.apply_compfile(cfg, comp_dir)
        cfg["run"]["comp_dir"] = comp_dir
        if not cfg["run"]["comp"]:
            cfg["run"]["comp"] = Path(comp_dir).name
    if getattr(args, "state_dir", None):
        cfg["run"]["state_dir"] = args.state_dir
        cfg["run"]["kill_file"] = str(Path(args.state_dir) / "KILL")
    if getattr(args, "dry_run", False):
        cfg["run"]["dry_run"] = True
    if getattr(args, "event_min", None):
        cfg["red"]["duration_min"] = args.event_min
        config_mod.validate_config(cfg)
    return cfg


def cmd_run(args):
    cfg = _load(args)
    result = run_mod.run_event(cfg, comp_dir=cfg["run"].get("comp_dir"),
                               max_cycles=args.cycles)
    print(json.dumps(result["summary"], indent=2, default=str))
    print(f"cycles={result['cycles']} reports={result['reports']['report']}")
    return 0


def cmd_seed(args):
    cfg = _load(args)
    ctx, _, _ = run_mod.build_context(cfg, comp_dir=cfg["run"].get("comp_dir"))
    outcome = seed_mod.seed_range(ctx, depth=args.depth, teams=args.team,
                                  dry_run=args.dry_run)
    print(json.dumps({"mode": outcome["mode"], "steps": outcome["steps"],
                      "manifest": outcome.get("manifest", {}).get("path"),
                      "boxes": {ip: {"stages": rec.get("stages"),
                                     "os": rec.get("os")}
                                for ip, rec in (outcome.get("boxes") or {}).items()}},
                     indent=2, default=str))
    return 0


def cmd_status(args):
    cfg = _load(args)
    print(json.dumps(run_mod.status(cfg, cfg["run"].get("comp_dir")), indent=2,
                     default=str))
    return 0


def cmd_candidates(args):
    cfg = _load(args)
    ctx, policy, _ = run_mod.build_context(cfg, comp_dir=cfg["run"].get("comp_dir"))
    candidates = policy.candidates(ctx)
    if args.json:
        print(json.dumps([dict(c) for c in candidates], indent=2, default=str))
    else:
        for candidate in candidates:
            print(f"{candidate['score']:7.2f}  {candidate['id']:44s} "
                  f"{candidate['reason']}")
    return 0


def cmd_decide(args):
    cfg = _load(args)
    ctx, policy, decider = run_mod.build_context(cfg, comp_dir=cfg["run"].get("comp_dir"))
    if args.execute:
        loop = run_mod.Loop(ctx, policy=policy, decider=decider, sensors=ctx.sensors)
        loop.guard_host()
        print(json.dumps(loop.step(), indent=2, default=str))
        return 0
    candidates = policy.candidates(ctx)
    digest = policy.frame(ctx, candidates)
    decision = decider.next_effect(1, digest, candidates)
    print(json.dumps({"digest": digest, "decision": decision.as_log(),
                      "options": [c["id"] for c in candidates]},
                     indent=2, default=str))
    return 0


def cmd_report(args):
    cfg = _load(args)
    ctx, policy, _ = run_mod.build_context(cfg, comp_dir=cfg["run"].get("comp_dir"))
    paths = report_mod.write_artifacts(cfg["run"]["state_dir"], ctx.world,
                                      policy=policy, sensors=ctx.sensors,
                                      out_dir=args.out)
    print(json.dumps(paths, indent=2))
    return 0


def cmd_restore_table(args):
    cfg = _load(args)
    ctx, _, _ = run_mod.build_context(cfg, comp_dir=cfg["run"].get("comp_dir"))
    if args.commands:
        print(json.dumps(report_mod.restore_commands(ctx.world), indent=2))
    else:
        print(json.dumps(report_mod.restore_table(ctx.world), indent=2))
    return 0


def cmd_verify_config(args):
    if args.print_contract:
        print(json.dumps(list(CONFIG_CONTRACT), indent=2))
        return 0
    cfg = _load(args)
    pushed = None
    if args.pushed:
        pushed = config_mod.load_config(args.pushed)
    problems = verify_config(cfg, pushed=pushed)
    contract = [{"field": f,
                 "effective": _dotted(cfg, f)} for f in CONFIG_CONTRACT]
    print(json.dumps({"contract": contract, "problems": problems,
                      "ok": not problems}, indent=2, default=str))
    return 1 if problems else 0


def cmd_c2_prep(args):
    cfg = _load(args)
    comp_dir = Path(cfg["run"].get("comp_dir") or ".")
    if not cfg["c2"].get("enabled"):
        print(json.dumps({"ok": False,
                          "error": "c2.enabled is false — set the C2 endpoint "
                                   "before comp-prep"}))
        return 2
    ctx, _, _ = run_mod.build_context(cfg, comp_dir=comp_dir)
    client = ctx.c2
    engine = str(cfg["c2"].get("engine") or "sliver").lower()
    out_dir = comp_dir / "red" / "payloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    transports = _transports_arg(args.transports)
    manifest = {"comp": cfg["run"].get("comp"),
                "operation": cfg["c2"].get("operation") or "sliver",
                "engine": engine, "dns_domain": cfg["c2"].get("dns_domain"),
                "transports": transports, "boxes": {}}
    if engine == "sliver":
        _prep_sliver(ctx, client, out_dir, manifest, transports,
                     write=not args.no_download)
    else:
        for box in ctx.intel.boxes():
            box_id = f"{box['box']}-{box['identifier']}"
            for transport in transports:
                entry = client.build_payload(
                    box_id, "windows" if box["windows"] else "linux",
                    transport=transport)
                entry["file"] = f"{box_id}-{transport}.bin"
                manifest["boxes"][f"{box_id}:{transport}"] = entry
                if not args.no_download:
                    client.download_payload(entry["uuid"], out_dir / entry["file"])
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "manifest": str(manifest_path),
                      "payloads": len(manifest["boxes"])}, indent=2))
    return 0


def _transports_arg(raw):
    from .c2 import TRANSPORTS
    names = [name.strip() for name in str(raw or "all").split(",") if name.strip()]
    if not names or names == ["all"]:
        return list(TRANSPORTS)
    unknown = [name for name in names if name not in TRANSPORTS]
    if unknown:
        raise ConfigError(f"unknown Sliver transport(s) {unknown}; "
                          f"choose from {list(TRANSPORTS)}")
    return names


def _prep_sliver(ctx, client, out_dir, manifest, transports, *, write=True):
    from .c2 import PIVOT_TRANSPORTS
    client.ensure_listeners()
    beacons = {"mtls", "http"} & set(transports)
    for box in ctx.intel.boxes():
        box_id = f"{box['box']}-{box['identifier']}"
        os_name = "windows" if box["windows"] else "linux"
        gateway = f"192.168.{box['identifier']}.1"
        for transport in transports:
            if transport == "named-pipe" and os_name != "windows":
                continue
            kinds = [(False, transport)]
            if transport in beacons:
                kinds.append((True, f"{transport}-beacon"))
            for beacon, key_transport in kinds:
                pivot_host = box["ip"] if transport in PIVOT_TRANSPORTS else None
                entry = client.build_payload(
                    box_id, os_name, transport=transport, gateway=gateway,
                    pivot_host=pivot_host, beacon=beacon)
                suffix = "exe" if os_name == "windows" else "bin"
                entry["file"] = f"{entry['name']}.{suffix}"
                entry["pivot"] = transport in PIVOT_TRANSPORTS
                # Pivots need a parent listener and beacons are the alternate
                # control path; both are built and shipped, not seeded blind.
                entry["plant"] = not beacon and transport not in PIVOT_TRANSPORTS
                data = entry.pop("data")
                if write:
                    (out_dir / entry["file"]).write_bytes(data)
                manifest["boxes"][f"{entry['name']}"] = entry


def build_parser():
    parser = argparse.ArgumentParser("tlaloc", description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="config file (default: repo config.yaml)")
    parser.add_argument("--comp", help="competition directory (Compfile + intel)")
    parser.add_argument("--state-dir", help="state directory (default: run.state_dir)")
    parser.add_argument("--dry-run", action="store_true",
                        help="no box is touched; every effect reports what it would do")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the loop")
    run_p.add_argument("--cycles", type=int, default=None)
    run_p.set_defaults(func=cmd_run)

    seed_p = sub.add_parser("seed", help="assume-breach seeding (pipeline stage)")
    seed_p.add_argument("--depth", type=int, default=None, help="0..3")
    seed_p.add_argument("--team", action="append", help="restrict to a team")
    seed_p.set_defaults(func=cmd_seed)

    sub.add_parser("status", help="what the agent believes right now") \
        .set_defaults(func=cmd_status)

    cand_p = sub.add_parser("candidates", help="the bounded option list")
    cand_p.add_argument("--json", action="store_true")
    cand_p.set_defaults(func=cmd_candidates)

    decide_p = sub.add_parser("decide", help="one decision (optionally execute it)")
    decide_p.add_argument("--execute", action="store_true")
    decide_p.set_defaults(func=cmd_decide)

    report_p = sub.add_parser("report", help="write the reports now")
    report_p.add_argument("--out", default=None)
    report_p.set_defaults(func=cmd_report)

    table_p = sub.add_parser("restore-table", help="the undo table for this event")
    table_p.add_argument("--commands", action="store_true")
    table_p.set_defaults(func=cmd_restore_table)

    vc_p = sub.add_parser("verify-config",
                          help="diff the effective config against what red01 was given")
    vc_p.add_argument("--pushed", help="the config file the deploy pipeline wrote")
    vc_p.add_argument("--print-contract", action="store_true")
    vc_p.add_argument("--event-min", type=int, default=None)
    vc_p.set_defaults(func=cmd_verify_config)

    prep_p = sub.add_parser("c2-prep", help="build + freeze the payload matrix")
    prep_p.add_argument("--transports", default="all",
                        help="comma list, or all (every Sliver transport)")
    prep_p.add_argument("--no-download", action="store_true")
    prep_p.set_defaults(func=cmd_c2_prep)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, TlalocError) as exc:
        print(f"tlaloc: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
