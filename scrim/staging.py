import subprocess
from pathlib import Path

from scrim import core
from scrim import procs
from scrim.core import log
from utils import load_users_config


DEFAULT_TEMPLATE = core.REPO / "competitions" / "agent-scrim"


RUNTIME_FILES = {
    "teams.json", ".deploy_state.json", "credentials.txt", "nakon-config.json",
    "packet.md", "event.conf",
}


def stage_author(args):
    src = Path(args.from_template or DEFAULT_TEMPLATE)
    if not src.is_dir():
        valid = sorted(p.name for p in (core.REPO / "competitions").iterdir() if p.is_dir())
        raise RuntimeError(
            f"template {src} does not exist. Pass --from-template with one of "
            f"{valid} (the default template itself is gone — competitions/ dirs are "
            f"curated; point DEFAULT_TEMPLATE at one that ships Compfile + boxes.json).")
    dst = core.REPO / "competitions" / args.new
    if dst.exists():
        raise RuntimeError(f"{dst} already exists")
    log(f"authoring {dst} from template {src}")
    (core.REPO / "competitions" / args.new).mkdir(parents=True)
    for item in src.iterdir():
        if item.name in RUNTIME_FILES or item.name in ("injects", "LOG.md") \
                or item.name.startswith("sub-") or item.name.startswith(".nakon-domain-"):
            continue
        # deploy.py writes `.postclone-swept`; this listed the long-dead `.phase6-swept`
        # name, so the real marker was copied into every competition authored from a
        # swept template and the new range skipped its post-clone sweep (audit find 12).
        if item.name == ".postclone-swept":
            continue
        if item.is_dir():
            subprocess.run(["cp", "-r", str(item), str(dst / item.name)], check=True)
        else:
            (dst / item.name).write_bytes(item.read_bytes())
    injects_src = src / "injects"
    if injects_src.exists():
        (dst / "injects").mkdir(exist_ok=True)
        for item in injects_src.iterdir():
            subprocess.run(["cp", "-r", str(item), str(dst / "injects" / item.name)], check=True)
    compfile = (dst / "Compfile").read_text().splitlines()
    compfile[0] = f"name {args.new}"
    (dst / "Compfile").write_text("\n".join(compfile) + "\n")
    if not (dst / "injects").exists():
        log("WARNING: template ships no injects/ — blue's inject work is structurally "
            "impossible on this comp (2026-10-03 scrim-fresh-a: injects gate n/a and "
            "half of blue's job missing). Author at least two under "
            f"{dst / 'injects'} before the event.")
    log("authored (scenario/creds carry over; edit Compfile/box_vulns.json to re-theme)")


def creds_from_files(comp):
    """All secrets/logins from the post-deploy artifacts (no terraform output needed)."""
    state = core.read_comp_json(comp, ".deploy_state.json")
    teams = core.read_comp_json(comp, "teams.json")
    engine_ip = core.engine_ip_from(comp)
    box_username, _credlist = load_users_config(comp)
    return {
        "ENGINE_IP": engine_ip,
        "ADMIN_PW": state["admin_password"],
        "INJECT_PW": state.get("inject_password") or "",
        "BOX_PW": state["box_password"],
        "BOX_USER": box_username,
        "KEY_PATH": str((core.REPO / "proxmox").resolve()),
        "VM_USER": core.vm_username(),
        **{f"{k.upper()}_PW": v["password"] for k, v in teams.items()},
        **{f"{k.upper()}_ID": v["identifier"] for k, v in teams.items()},
    }


def stage_deploy(args, comp):
    cmd = ["python3", "-u", "create-competition.py", "--competition", comp.name,
           "--teams", str(args.teams), "--yes"]
    if comp.joinpath(".deploy_state.json").exists() and args.resume:
        cmd += ["--from-phase", str(args.resume)]
        # --force-resume must reach create-competition: it is the operator's answer to
        # the resume-loop guard; without the pass-through the harness accepted the flag
        # while the deploy refused anyway (2026-10-04).
        if args.force_resume:
            cmd += ["--force-from-phase"]
    r = procs.run(cmd, cwd=core.REPO, timeout=6 * 3600, check=False)
    if r.returncode != 0 and args.run_dir:
        # stderr too: the phase-4 checkpoint SystemExit and a phase-3 ssh timeout both
        # printed ONLY to stderr, and a stdout-only deploy.log sent the post-mortem to
        # the live estate for the answer (live-found 2026-10-03, twice).
        (Path(args.run_dir) / "deploy.log").write_text(
            (r.stdout or "") + "\n=== STDERR ===\n" + (r.stderr or ""))
    for line in (r.stdout or "").splitlines()[-15:]:
        print("   ", line)
    if r.returncode != 0:
        raise RuntimeError(f"deploy failed rc={r.returncode} (log above); fix and resume with --skip-deploy/--from-phase")
    # The pipeline's final phase number is the source of truth — hardcoded 7s went
    # stale when firewalls became phase 5 and seed became phase 8 (live-found
    # 2026-10-03: a completed deploy was rejected as "did not reach phase 7").
    from deploy_lib.phases import PHASES
    final_phase = len(PHASES)
    if f"last_phase: {final_phase}" not in (r.stdout or ""):
        state = core.read_comp_json(comp, ".deploy_state.json")
        if state.get("last_phase") != final_phase:
            raise RuntimeError(f"deploy did not reach phase {final_phase}")
