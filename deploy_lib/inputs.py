"""Read the competition's on-disk inputs: engine vmid, Compfile/boxes/users, prior state, injects.

Loading only — every refusal (missing state, version, resume guards) lives in gates.py and is
invoked from prepare().
"""

import json
import os
import sys

from config.config_ops import load_boxes, load_packet_passwords
from config.injects_config import load_injects
from common.constants import SCORING_ENGINE_VMID
from common.utils import compfile_flag, env_summary, load_compfile, load_users_config


def resolve_engine_vmid(ctx, comp_dir, from_phase, scoring_vmid):
# Settle the scoring-engine VMID into `ctx` (it is resolved first: the lock keys on it).
#
#     A corrupt or absent .deploy_state.json here falls back to the default rather than
#     raising: load_prior_deploy_state reads the same file a few steps later and does the
#     refusing, with a message that names the actual problem.
    # Resolve this competition's scoring-engine VMID before taking the engine lock
    # (the lock is keyed on it) and before phase 1 runs. On resume it
    # is authoritative from .deploy_state.json; on a fresh deploy it comes from the
    # flag/arg (default SCORING_ENGINE_VMID).
    _early_state = comp_dir / ".deploy_state.json"
    if from_phase > 1 and _early_state.exists():
        try:
            ctx.engine_vmid = int(
                json.loads(_early_state.read_text()).get("scoring_vm_id", SCORING_ENGINE_VMID))
        except (ValueError, json.JSONDecodeError):
            ctx.engine_vmid = SCORING_ENGINE_VMID
    else:
        # flag wins, then TF_VAR_scoring_vm_id from the environment (env variants ship
        # it; live-found 2026-10-03 it was silently ignored and the default 1000 was
        # taken instead — invisible until a second range on the node collided),
        # then the built-in default.
        if scoring_vmid is not None:
            ctx.engine_vmid = int(scoring_vmid)
        else:
            try:
                ctx.engine_vmid = int(os.environ.get("TF_VAR_scoring_vm_id")
                                           or SCORING_ENGINE_VMID)
            except ValueError:
                ctx.engine_vmid = SCORING_ENGINE_VMID


def load_competition_spec(ctx, comp_dir):
# Load the Compfile, users.json and boxes.json into `ctx`; print the banner.
#
#     A missing boxes.json is fatal here: every later step (and every phase) enumerates
#     boxes. Known-broken templates only warn — the competition may predate the fix, and
#     the deploy still has to be able to refuse later on its own terms.
    ctx.name, ctx.scenario, ctx.difficulty = load_compfile(comp_dir / "Compfile")
    # A comp dir that arrived by git (stage_author authoring, manual copy, fresh clone)
    # lacks compile-packet's gitignored secret layer; without it the machine list has
    # no packet-promised decoy/local accounts and the harness's pre-T0 verify --packet
    # aborts the run (live-found 2026-10-04, scrim-reset). Compile-on-demand here.
    from config.packet_ops import ensure_packet_secrets
    ensure_packet_secrets(comp_dir)
    ctx.box_username, ctx.credlist_usernames = load_users_config(comp_dir)
    # nakon --jobs pass-through (M1.2): per-machine work is atomic in nakon's runner, so N
    # machines plant concurrently with each machine's step order (disruptive last) intact.
    # The ceiling is engine egress/CPU and mirror throughput, not the datastore (no bulk
    # writes) — start at 4 and judge against the M0.2 timings.
    ctx.nakon_jobs = max(1, compfile_flag(comp_dir / "Compfile", "nakon_jobs", 4))
    # apt_cache (M1.3): point each box's apt at the engine's apt-cacher-ng mirror cache.
    # Approved trade-off: through the IP-based proxy, resolv-conf-null-dns no longer breaks
    # apt (it still breaks every other resolver user on the box); empty-sources/hold
    # configs break apt either way. Set `apt_cache 0` in the Compfile for full realism.
    ctx.apt_cache = bool(compfile_flag(comp_dir / "Compfile", "apt_cache", 1))
    ctx.comp_name = comp_dir.name
    print(f"\n{'='*60}")
    print(f"  Deploying {ctx.comp_name}")
    print(f"  {env_summary()}")
    print(f"{'='*60}\n")

    ctx.boxes = load_boxes(comp_dir)
    if not ctx.boxes:
        print("  ERROR: No boxes.json found. Create a new competition or add boxes.json.")
        sys.exit(1)


def load_prior_deploy_state(ctx, comp_dir, from_phase):
# Read .deploy_state.json into `ctx` and decide whether this is a resume.
#
#     A corrupt or unreadable file reads as empty state. The refusals that depend on the
#     file (missing on resume, pipeline version, skipped phases, resume-loop, missing
#     machines) are gates.check_resume_gates, which prepare() runs right after this.
    ctx.state_path = comp_dir / ".deploy_state.json"
    ctx.previous_state = {}
    if ctx.state_path.exists():
        try:
            ctx.previous_state = json.loads(ctx.state_path.read_text())
        except (ValueError, OSError):
            ctx.previous_state = {}
    ctx.resuming = from_phase > 1


def load_competition_inputs(ctx, comp_dir):
# Load injects and the packet-published credentials into `ctx`.
#
#     Loaded after the resume guards and before the secrets step: passwords.json outranks
#     both state and a fresh mint, and the secrets step needs the packet's derived
#     credlists, so the two inputs are materialised together in one place.
    ctx.injects = load_injects(comp_dir)
    # Packet-published credentials (compile-packet.py -> passwords.json): when present,
    # box_password and the credlists are the packet's default credentials verbatim, not
    # random mints. Teams get them in the packet and rotate at minute zero — that IS the
    # competition. Also makes redeploy deterministic (no re-minted secret drifting from
    # the packet).
    ctx.packet_pw = load_packet_passwords(comp_dir)
    ctx.packet_credlists = (ctx.packet_pw or {}).get("credlists") or {}
