"""Redeploy modes that keep the VM: resync, snapshot rollback (ready/base), reconfigure."""

import json
import shlex
import pipeline_api

from estate import state as estate_state

from common.constants import SNAP_BASE, SNAP_READY, WINDOWS_ADMIN_USER
from engine.engine_event_ops import read_event_conf
from infra.guest_exec import guest_agent_exec_root, guest_agent_exec_windows
from infra.lifecycle import (delete_snapshot, list_snapshots, rollback_snapshot, take_snapshot,)
from common.targets import describe_target
from common.utils import load_users_config
from redeploy.redeploy_select_ops import box_platform
from redeploy.redeploy_plant_ops import run_nakon_and_harden, rerun_domain_configs


def mode_resync(targets, rctx):
# Align the state document with the secrets the engine holds, then re-set the selected boxes'
#     passwords via the guest agent. box_password is baked into the boxes and exists nowhere
#     on the engine, so it is reported, not repaired.
    ctx, node, comp_dir, state = (
        rctx.ctx, rctx.node, rctx.comp_dir, rctx.state)
    print("  Reading engine-authoritative secrets (event.conf, credlist, /opt/quotient/.env)...")
    secrets = read_event_conf(ctx)

    changed = []
    for key in ("admin_password", "postgres_password", "redis_password", "inject_password"):
        new = secrets.get(key)
        if new and state.get(key) != new:
            print(f"    {key}: state {'drifted' if state.get(key) else 'missing'} -> aligned with engine")
            state[key] = new
            changed.append(key)
    if secrets.get("box_creds") and state.get("box_creds") != secrets["box_creds"]:
        print("    box_creds: drifted -> aligned with engine credlist")
        state["box_creds"] = secrets["box_creds"]
        changed.append("box_creds")
    for team_key, pw in (secrets.get("team_passwords") or {}).items():
        entry = (state.get("teams") or {}).get(team_key)
        if pw and entry and entry.get("password") != pw:
            print(f"    {team_key} password: drifted -> aligned with engine")
            entry["password"] = pw
            changed.append(f"teams.{team_key}.password")
    if not changed:
        print("    the state document already matches the engine.")
    if changed:
        estate_state.write_flat(comp_dir, state)

    box_password = state.get("box_password")
    if not box_password:
        raise SystemExit(
            "  ERROR: the state document has no box_password — cannot re-set box logins. "
            "Only the state alignment above was applied.")
    box_username, _credlist = load_users_config(comp_dir)
    for t in targets:
        tnode = t.get("node") or node
        try:
            if box_platform(t["box"]) == "windows":
                rc, out, err = guest_agent_exec_windows(
                    tnode, t["vmid"],
                    f"net user {WINDOWS_ADMIN_USER} '{box_password}'", timeout=120)
            else:
                script = f"echo {shlex.quote(f'{box_username}:{box_password}')} | chpasswd"
                for user, pw in (state.get("box_creds") or {}).items():
                    script += f"; echo {shlex.quote(f'{user}:{pw}')} | chpasswd"
                rc, out, err = guest_agent_exec_root(tnode, t["vmid"], script, timeout=120)
            if rc == 0:
                print(f"    {describe_target(t)}: passwords re-set (via guest agent)")
            else:
                print(f"    WARNING: {describe_target(t)}: guest agent rc={rc}: {(err or '').strip()[:120]}")
        except Exception as e:
            print(f"    WARNING: {describe_target(t)}: password re-set failed ({e})")

    print("  NOTE: box_password itself cannot be recovered from the engine — if the box "
          "login (as opposed to credlist accounts) is what drifted, reset it by hand.")
    return targets


def mode_rollback(targets, rctx, snapshot, nakon_config_path, nakon_bundle, reconfigure):
    ctx, node = rctx.ctx, rctx.node
    restored = []
    for t in targets:
        print(f"  Rolling back {describe_target(t)} to '{snapshot}'...")
        try:
            tnode = t.get("node") or node
            if snapshot == SNAP_BASE:
                # PVE rolls a disk back only to its most recent snapshot, so reaching the
                # pre-plant disk means dropping tz-ready first; the replant below re-takes it.
                snaps = list_snapshots(tnode, t["vmid"])
                if SNAP_READY in snaps:
                    print(f"    dropping newer '{SNAP_READY}' (PVE rolls back only to the "
                          f"most recent snapshot; it is re-taken after the replant)")
                    delete_snapshot(tnode, t["vmid"], SNAP_READY)
            rollback_snapshot(tnode, t["vmid"], snapshot)
            restored.append(t)
            print(f"    {t['vm_name']} restored")
        except Exception as e:
            print(f"  WARNING: rollback failed for {describe_target(t)}: {e}")

    if not restored:
        raise SystemExit("  ERROR: no box was rolled back successfully — nothing to do.")

    pipeline_api.wait_for_boxes_ssh(ctx, restored, timeout=300)

    if reconfigure:
        pipeline_api.wait_for_cloud_init(ctx, restored, timeout=240)
        run_nakon_and_harden(restored, rctx, nakon_config_path, nakon_bundle)
        try:
            domain_settled = rerun_domain_configs(restored, rctx, nakon_config_path)
        except Exception:
            print("  tz-ready NOT re-taken — the domain chain failed above, so the boxes are "
                  "not in an 'as delivered' state.")
            raise
        if domain_settled:
            print(f"  Re-taking '{SNAP_READY}' for the recovered boxes...")
            for t in restored:
                take_snapshot(t.get("node") or node, t["vmid"], SNAP_READY,
                              description="tezcatlipoca: as delivered (re-taken by redeploy)")
        else:
            print("  tz-ready NOT re-taken — domain configuration could not run (see above); "
                  "snapshotting now would bake a broken state in as 'as delivered'.")

    return restored


def mode_reconfigure(targets, rctx, nakon_config_path, nakon_bundle):
    ctx, comp_dir = rctx.ctx, rctx.comp_dir
    # 900s: the operator->engine->box jump path has banner-timeout flakiness, and a shorter
    # shared budget aborts scoping runs on boxes the engine reaches fine.
    pipeline_api.wait_for_boxes_ssh(ctx, targets, timeout=900)
    run_nakon_and_harden(targets, rctx, nakon_config_path, nakon_bundle)
    roles_path = comp_dir / "domain_roles.json"
    if roles_path.exists():
        roles = json.loads(roles_path.read_text())
        if any(roles.get(t["box_name"]) for t in targets):
            print("  NOTE: reconfigure never resets disks, so domain membership is assumed "
                  "intact. If the AD domain itself is what's broken, use --mode rollback-base "
                  "(or rebuild) — those re-promote/re-join after the reset.")
    return targets
