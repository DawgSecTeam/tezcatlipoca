import subprocess
from pathlib import Path

from estate import config
from estate import state as estate_state
from scrim import core
from scrim import procs
from scrim.core import log
from common.comp_paths import comps_root, resolve_comp_dir
from common.utils import load_users_config


DEFAULT_TEMPLATE = "agent-scrim"  # a competition name under the comps root


RUNTIME_FILES = {
    "teams.json", "estate-state.json", "credentials.txt",
    "nakon-config.json", "packet.md", "event.conf",
}


def warn_co_tenancy(args, comp):
    """Warn when ANOTHER tezcatlipoca range is running on this run's node.

    A live event's guests share the node's networking with every other range: a concurrent deploy
    or teardown creates/destroys bridges and runs `ifreload -a` node-wide. caught live
    : red01 went unreachable at T+13 and stayed gone for 30 of the event's 45
    minutes, six red gates and RED-TEAM.md with it; a second session's scrim plus ten
    deploy/destroy cycles on the same node bracket the exact minute, and nothing in the run log
    said a word about the co-tenancy. Nothing here REFUSES — concurrent runs are deliberate — but
    the run has to record what it shared the node with.
    """
    node = config.active().proxmox.node
    if not node:
        return None
    try:
        from infra.client import proxmox_api
        resources = (proxmox_api("GET", "/cluster/resources", params={"type": "vm"}) or {}).get("data") or []
    except Exception as e:                                   # noqa: BLE001
        log(f"co-tenancy check skipped ({type(e).__name__}: {e})")
        return None
    our_run = ""
    try:
        our_run = core.read_state(comp).get("run_id") or ""
    except Exception:                                        # noqa: BLE001
        our_run = ""
    others = {}
    for res in resources:
        tags = str(res.get("tags") or "")
        if res.get("node") != node or res.get("status") != "running" or "tezcatlipoca" not in tags:
            continue
        if our_run and our_run in tags:
            continue
        key = next((t for t in str(res.get("tags") or "").replace(";", ",").split(",")
                    if t.startswith("run-")), "untagged")
        others[key] = others.get(key, 0) + 1
    if not others:
        log(f"co-tenancy: no other tezcatlipoca range is running on {node} (our range: {our_run or 'untagged'})")
        return {}
    log("WARNING: another tezcatlipoca range is RUNNING on "
        + node + " (" + ", ".join(f"{k}: {n} guest(s)" for k, n in sorted(others.items()))
        + ") — its deploys/teardowns bounce the node's networking and can drop this range's guests "
          "mid-event (one run lost red01 exactly that way). Prefer an idle node for a live event.")
    return others


def stage_author(args):
    src = Path(args.from_template or DEFAULT_TEMPLATE)
    # A bare --from-template NAME names a competition under the comps root, not a
    # cwd-relative path: full-scrim-20261013d died in authoring because Path(NAME)
    # resolved against the driver's cwd and missed the worktree's competitions dir.
    if not src.is_absolute() and not src.is_dir():
        src = comps_root() / src
    if not src.is_dir():
        valid = sorted(p.name for p in comps_root().iterdir() if p.is_dir())
        raise RuntimeError(
            f"template {src} does not exist. Pass --from-template with one of "
            f"{valid} (the default template itself is gone — competitions/ dirs are "
            f"curated; point DEFAULT_TEMPLATE at one that ships Compfile + boxes.json).")
    dst = resolve_comp_dir(args.new, getattr(args, "comp_dir", None))
    if dst.exists():
        raise RuntimeError(f"{dst} already exists")
    log(f"authoring {dst} from template {src}")
    dst.mkdir(parents=True)
    for item in src.iterdir():
        if item.name in RUNTIME_FILES or item.name in ("injects", "LOG.md") \
                or item.name.startswith("sub-") or item.name.startswith(".nakon-domain-"):
            continue
        # A swept template must not carry the marker into a competition authored from it:
        # the new range would skip its own post-clone sweep (a harness audit finding).
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
            "impossible on this comp (injects gate n/a and "
            "half of blue's job missing). Author at least two under "
            f"{dst / 'injects'} before the event.")
    log("authored (scenario/creds carry over; edit Compfile/box_vulns.json to re-theme)")


def creds_from_files(comp):
    state = estate_state.load_flat(comp)
    teams = core.read_comp_json(comp, "teams.json")
    engine_ip = core.engine_ip_from(comp)
    box_username, _credlist = load_users_config(comp)
    return {
        "ENGINE_IP": engine_ip,
        "ADMIN_PW": state["admin_password"],
        "INJECT_PW": state.get("inject_password") or "",
        "BOX_PW": state["box_password"],
        "BOX_USER": box_username,
        "KEY_PATH": core.ssh_key_path(),
        "VM_USER": core.vm_username(),
        **{f"{k.upper()}_PW": v["password"] for k, v in teams.items()},
        **{f"{k.upper()}_ID": v["identifier"] for k, v in teams.items()},
    }


def stage_deploy(args, comp):
    # --comp-dir is the whole point of the dashboard's per-run workspace: the comp lives OUTSIDE
    # the engine tree there, so a bare `--competition <dir name>` resolved under the comps root,
    # found nothing, and scaffolded a NEW competition called "comp" instead of reusing this one
    # (interactive prompt → EOFError). `--competition` carries the
    # run's real comp id when the caller gave one (the workspace dir is just "comp").
    name = getattr(args, "competition", None) or comp.name
    cmd = [core.local_python(), "-u", "create-competition.py", "--competition", name,
           "--comp-dir", str(comp), "--teams", str(args.teams), "--yes"]
    if getattr(args, "nodes", None):
        # The dashboard hands every action the run's node list; without this the deploy
        # stage silently targeted whatever TEZ_ENGINE_ENV_FILE points at .
        cmd += ["--nodes", str(args.nodes)]
    # Placement inputs travel with the scrim, not just the env: a scrim that deploys while
    # another range holds the env's engine vmid / team block / management IP would collide.
    for flag, key in (("--scoring-vmid", "scoring_vmid"),
                      ("--team-identifiers", "team_identifiers"),
                      ("--engine-mgmt-ip", "engine_mgmt_ip")):
        value = getattr(args, key, None)
        if value:
            cmd += [flag, str(value)]
    if estate_state.read_flat(comp) is not None and args.resume:
        cmd += ["--from-phase", str(args.resume)]
        # --force-resume must reach create-competition: it is the operator's answer to
        # the resume-loop guard; without the pass-through the harness accepted the flag
        # while the deploy refused anyway .
        if args.force_resume:
            cmd += ["--force-from-phase"]
    r = procs.run(cmd, cwd=core.REPO, timeout=6 * 3600, check=False)
    if args.run_dir:
        # ALWAYS, success included: the phase-by-phase log is the only durable record of what
        # the deploy did — the template-reuse decisions ("Engine template reuses on the node's
        # hash stamp", "golden X hash differs — rebuilding") and every phase's output live here
        # and nowhere else. Written only on failure early on, so a SUCCESSFUL run left
        # no evidence of the reuse it had just proved (on the run that found it: no deploy.log anywhere in
        # the run dir). stderr too: the phase-4 checkpoint SystemExit and a phase-3 ssh timeout
        # both printed ONLY to stderr, and a stdout-only log sent the post-mortem to the live
        # estate for the answer (caught live, twice).
        (Path(args.run_dir) / "deploy.log").write_text(
            (r.stdout or "") + "\n=== STDERR ===\n" + (r.stderr or ""))
    for line in (r.stdout or "").splitlines()[-15:]:
        print("   ", line)
    if r.returncode != 0:
        log_path = (Path(args.run_dir) / "deploy.log") if args.run_dir else None
        tail = (r.stderr or "").strip()[-1200:]
        raise RuntimeError(
            f"deploy failed rc={r.returncode}"
            + (f"; stderr tail: {tail}" if tail else "")
            + (f"; full log: {log_path}" if log_path else "")
            + "; fix and resume with --skip-deploy/--from-phase")
    # The pipeline's final phase number is the source of truth — hardcoded 7s went
    # stale when firewalls became phase 5 and seed became phase 8 (live-found
    # caught live: a completed deploy was rejected as "did not reach phase 7").
    from deploy_lib.phases import PHASES
    final_phase = len(PHASES)
    if f"last_phase: {final_phase}" not in (r.stdout or ""):
        state = estate_state.load_flat(comp)
        if state.get("last_phase") != final_phase:
            raise RuntimeError(f"deploy did not reach phase {final_phase}")
