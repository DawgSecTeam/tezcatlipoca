"""Deploy, verify, and run an agent-manned red-vs-blue scrim end to end."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from config.config_ops import write_state
from scrim import blue_agent
from scrim import blue_watchdog
from scrim import core
from scrim import event_run
from scrim import fire_test
from scrim import inject_sync
from scrim import lock_ops
from scrim import procs
from scrim import red_stage
from scrim import run_manifest
from scrim import staging
from scrim import test_folder
from scrim.core import log
from common.comp_paths import resolve_comp_dir
from common.utils import valid_comp_name
from estate import config


def main():
    # Start-up umask: every evidence file, jar and log this run creates is private unless
    # a call site explicitly widens it. Several evidence writes used to rely on the
    # operator's umask (audit find D5).
    os.umask(0o077)
    # The deploy child reads .env itself, but the harness's IN-PROCESS paths (the
    # end-of-event teardown runner, compworld) resolve their config from this process's
    # environment: the run's teardown died with a missing TEZ_PROXMOX_ENDPOINT because
    # only the child had loaded .env.
    from dotenv import load_dotenv
    load_dotenv(core.REPO / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--competition", required=True, help="competition name (resolved under the "
                   "comps root: TEZ_COMPETITIONS_ROOT, else ../tezcatlipoca-competitions, "
                   "else competitions/)")
    p.add_argument("--comp-dir", dest="comp_dir", default=None,
                   help="path to the competition's dir (wins over --competition resolution)")
    p.add_argument("--nodes", default=None,
                   help="path to this run's node list (the nodes.json schema); forwarded to the "
                        "deploy stage so the range lands on the listed hosts, not the profile's")
    p.add_argument("--scoring-vmid", dest="scoring_vmid", type=int, default=None,
                   help="this run's scoring-engine VMID, forwarded to the deploy stage; give "
                        "each concurrent range on a shared node its own so the engines differ")
    p.add_argument("--team-identifiers", dest="team_identifiers", default=None,
                   help="this run's team identifier block (subnet ids), forwarded to the deploy "
                        "stage; a concurrent range on the same node needs a non-overlapping one")
    p.add_argument("--engine-mgmt-ip", dest="engine_mgmt_ip", default=None,
                   help="this run's engine management IP, forwarded to the deploy stage; a "
                        "concurrent range on the same node cannot share one")
    p.add_argument("--new", help="author competitions/<NAME> from --from-template first")
    p.add_argument("--from-template", default=None)
    p.add_argument("--teams", type=int, default=2)
    p.add_argument("--duration-min", type=int, default=90)
    p.add_argument("--no-seed-red", dest="seed_red", action="store_false",
                   help="skip the pre-T0 assume-breach seed (no pre-planted beacons)")
    p.add_argument("--seed-depth", type=int, default=3, choices=[0, 1, 2, 3],
                   help="assume-breach seed depth: 0 access | 1 +implant | 2 +artifacts "
                        "| 3 +evasion (default 3)")
    p.add_argument("--blue-model", default="openai/gpt-5.6-luna", help="cheap cloud model for blue agents")
    p.add_argument("--red-model", default="openai/gpt-5.6-luna", help="cheap cloud model for bad-auto")
    p.add_argument("--reasoning-effort", default="",
                   help="reasoning effort sent to the LLM (GPT-5.x/o-series only — "
                        "the local qwen endpoints 500 on it); pass e.g. 'minimal' "
                        "for cloud reasoning models")
    p.add_argument("--llm-base-url", default="https://openrouter.ai/api/v1")
    p.add_argument("--blue-base-url", default=None,
                   help="LLM base URL for blues only (defaults to --llm-base-url); "
                        "e.g. the local qwen endpoint http://100.64.0.9:8080/v1")
    p.add_argument("--blue2-base-url", default=None,
                   help="separate LLM base URL for team2's blue (default: same as --blue-base-url)")
    p.add_argument("--blue2-model", default=None,
                   help="model for team2's blue when --blue2-base-url is set")
    # No default: `None` is what lets `red_stage._resolve_red_ip` consult the comp's own
    # reservation (Compfile `red01_ip`). Hardcoding bad-auto's own default here made that
    # resolver unreachable, and a re-stage moved red01 off the address the deploy recorded
    # and the realm C2 still pointed at (caught live: deploy planted at the legacy .196
    # default, re-stage re-selected the address). `--red-vmid` has always
    # defaulted to None for the same reason.
    p.add_argument("--red-ip", default=None,
                   help="red01 IP on the cluster; empty resolves the comp's own reservation "
                        "(Compfile red01_ip), else the tez default")
    p.add_argument("--red-gw", default="10.0.0.1", help="red01 gateway")
    p.add_argument("--red-storage", default="hdd", help="storage pool red01 clones from")
    p.add_argument("--red-vmid", type=int, default=None,
                   help="red01 vmid when the cluster default collides")
    p.add_argument("--red-template", default=config.active().knobs.red_template or None,
                   help="red01 template name when it differs from the cluster default "
                        "(default: $TEZ_RED_TEMPLATE — bad-auto's built-in default is a "
                        ".193 name; .150 wants base-ubuntu24.04-fix)")
    p.add_argument("--red-mode", choices=["routed", "masq"], default=None,
                   help="red's network identity: routed (bad-auto default) gives red01 a "
                        "dedicated segment and keeps its source IP visible end-to-end, so "
                        "blue can hunt and firewall the attacker while scoring keeps "
                        "sourcing from the team gateway; masq = gateway masquerade "
                        "(red unblockable-by-IP, indistinguishable from scoring)")
    p.add_argument("--red-subnet", default=None,
                   help="red segment CIDR in routed mode (bad-auto default 10.200.0.0/24); "
                        "must not overlap the team 192.168.0.0/16 or the mgmt LAN")
    p.add_argument("--red-seg-ip", default=None,
                   help="red01's address on the red segment (bad-auto default 10.200.0.10; "
                        "the engine takes the segment gateway x.x.x.1)")
    p.add_argument("--red-tunnel", choices=["auto", "on", "off"], default="auto",
                   help="reverse-SSH tunnel so red01 can reach a local LLM endpoint "
                        "(auto = on for non-openrouter endpoints)")
    p.add_argument("--skip-deploy", action="store_true", help="competition already at phase 7")
    p.add_argument("--resume-event", action="store_true", dest="resume_event",
                   help="the driver died mid-event: skip staging entirely and re-run the blue "
                        "feeds + monitor + capture + teardown from the T0 recorded in "
                        "run_dir/T0.txt (badauto red keeps running on red01 regardless)")
    p.add_argument("--from-phase", dest="resume", type=int, default=None,
                   help="resume create-competition at this phase")
    p.add_argument("--force-resume", action="store_true", dest="force_resume",
                   help="with --resume-event: proceed even when too little of the event "
                        "window is left for a single useful cycle (capture + teardown only)")
    p.add_argument("--keep-range", action="store_true", help="skip destroy-competition at teardown")
    p.add_argument("--keep-templates", action="store_true", dest="keep_templates",
                   help="teardown normally (team boxes, engine, bridges, red01) but KEEP the "
                        "golden + engine templates, so the next run of this competition reuses "
                        "the exact artifacts this practice run tested instead of rebuilding them")
    p.add_argument("--run-dir", default=None)
    p.add_argument("--blue-watchdog", action="store_true", dest="blue_watchdog",
                   help="run a non-LLM loop that re-unmasks/starts stopped scored Linux units every "
                        f"{blue_watchdog.WATCHDOG_INTERVAL}s — keeps availability up through a blue-agent API outage")
    args = p.parse_args()
    args.blue_base_url = args.blue_base_url or args.llm_base_url

    for nm in (args.competition, args.new):
        if nm and not valid_comp_name(nm):
            sys.exit(f"invalid competition name {nm!r} — use [a-z0-9._-], no path separators.")

    comp = resolve_comp_dir(args.competition, args.comp_dir)
    if args.resume_event:
        if not comp.is_dir():
            # resolve_run_dir opens/creates the test folder under comp, so a typo must not
            # mint a comp dir for a run that cannot exist.
            sys.exit(f"no such competition: {comp}")
        lock_ops.acquire_scrim_lock(comp)
        # The same folder the first run used: the test folder is keyed on this comp's run
        # id, and a paths.run_dir already recorded in test.json is authoritative, so a
        # resume can never mint a second key or fork the evidence (INV1).
        run_dir, test_dir = test_folder.resolve_run_dir(comp, args)
        log(f"test folder: {test_dir}")
        manifest = run_manifest.load_manifest(run_dir)
        run_manifest.resume_intent(args, manifest)
        t0 = None
        t0_file = run_dir / "T0.txt"
        if t0_file.exists():
            rec = json.loads(t0_file.read_text().strip())
            t0 = float(rec["t0"])
            args.duration_min = int(rec.get("duration_min") or args.duration_min)
        elif manifest.get("t0"):
            t0 = float(manifest["t0"])
            args.duration_min = int(manifest.get("duration_min") or args.duration_min)
        args.run_dir = str(run_dir)
        if t0 is None:
            # No T0.txt and no manifest t0: the driver died before the event clock
            # started (almost always inside stage_red, which deploys red01 for tens of
            # minutes). There is nothing to re-enter, and saying so beats a bare
            # "T0.txt missing" now that the pre-stage_red marker exists.
            where = f" (manifest phase={manifest.get('phase')!r})" if manifest else ""
            sys.exit(f"--resume-event: no event clock in {run_dir} (T0.txt and run.json "
                     f"t0 both missing){where} — the run never reached T0, so there is no "
                     f"event to resume. Re-stage with --skip-deploy instead.")
        remaining = args.duration_min - (time.time() - t0) / 60
        refusal = run_manifest.resume_refusal(remaining, force=args.force_resume)
        if refusal:
            sys.exit(refusal)
        log(f"RESUME: re-entering stage_run at T+{int((time.time() - t0) / 60)}min "
            f"({remaining:.0f}min left; keep_range={args.keep_range}, "
            f"keep_templates={args.keep_templates}, "
            f"watchdog={args.blue_watchdog} from the manifest)")
        creds = staging.creds_from_files(comp)
        creds["RUN_DIR"] = args.run_dir
        run_manifest.record_phase(run_dir, args, "event-resumed", t0=t0)
        failure = event_run.run_event_and_finish(args, creds, t0)
        log("DONE — resumed event captured and torn down")
        if failure:
            sys.exit(f"FAILED: {failure}")
        return
    if args.new:
        args.competition = args.new
        comp = resolve_comp_dir(args.new, args.comp_dir)
        staging.stage_author(args)
    if not (comp / "Compfile").exists():
        sys.exit(f"no such competition: {comp}")
    # Before any staging: one scrim per resolved range workspace (the deploy's engine lock
    # only covers engine-touching work, and a --skip-deploy scrim takes no engine lock).
    lock_ops.acquire_scrim_lock(comp)

    run_dir, test_dir = test_folder.resolve_run_dir(comp, args)
    run_dir.mkdir(parents=True, exist_ok=True)
    args.run_dir = str(run_dir)
    log(f"run dir: {run_dir}")
    log(f"test folder: {test_dir}")
    # stage_author ran before the run dir existed, so its marker lands here — the
    # dashboard's stage list must still open with `author`.
    if args.new:
        run_manifest.record_phase(run_dir, args, "author")

    if not args.skip_deploy:
        run_manifest.record_phase(run_dir, args, "deploy")
        staging.stage_deploy(args, comp)
    creds = staging.creds_from_files(comp)
    staging.warn_co_tenancy(args, comp)
    creds["RUN_DIR"] = args.run_dir
    log(f"engine {creds['ENGINE_IP']}, teams {[(k, v['identifier']) for k, v in json.loads((comp / 'teams.json').read_text()).items()]}")

    if not (comp / "packet.md").exists():
        log("packet.md missing — generating")
        procs.run([core.local_python(), "generate-packet.py", str(comp)], cwd=core.REPO, timeout=120)

    run_manifest.record_phase(run_dir, args, "verify")
    fire_test.stage_verify(args, comp, creds)
    run_manifest.record_phase(run_dir, args, "blues")
    blue_agent.stage_blues(args, comp, run_dir, creds, time.time())

    red_setup_started = time.time()
    # Marker BEFORE stage_red: red01 deployment takes tens of minutes, and a driver that
    # dies in there used to leave nothing but a missing T0.txt for --resume-event (D3).
    run_manifest.record_phase(run_dir, args, "red")
    red_stage.stage_red(args, comp, creds, run_dir)
    t0 = time.time()
    write_state(run_dir / "T0.txt", {"t0": t0, "duration_min": args.duration_min})
    run_manifest.record_phase(run_dir, args, "event", t0=t0)
    log(f"T0 — event clock starts now (red setup took {(t0 - red_setup_started) / 60:.0f} min, "
        f"outside scored time)")
    inject_sync.reanchor_injects(args, comp, creds)

    failure = event_run.run_event_and_finish(args, creds, t0)
    test_dir = getattr(args, "test_dir", None) or run_dir
    log(f"DONE — reports + evidence in {test_dir}; REPORT.md there needs its judgement "
        f"sections filled, then: python3 test-artifacts.py verify {args.competition} "
        f"{Path(test_dir).name} --seal")
    if failure:
        sys.exit(f"FAILED: {failure}")
