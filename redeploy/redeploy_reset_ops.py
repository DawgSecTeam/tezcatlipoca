"""Redeploy mode reset: per-box cheapest-that-works ladder (tz-ready, tz-base + replant, rebuild)."""

import json
import time

from common.constants import SNAP_BASE, SNAP_READY
from infra.guest_exec import wait_for_guest_agent
from infra.lifecycle import list_snapshots
from common.ssh_ops import classify_ssh_failure, ssh_on_gateway
from redeploy.redeploy_select_ops import box_platform
from redeploy.redeploy_plant_ops import prepare_nakon_assets
from redeploy.redeploy_light_ops import mode_rollback
from redeploy.redeploy_rebuild_ops import mode_rebuild


def scored_ports_for(comp_dir):
# Box name to sorted scored TCP ports. Bare catalog names resolve through quotient's own
#     _SERVICE_TO_CHECK — the same mapping that builds event.conf — and plant_only pins emit none.
    path = comp_dir / "box_services.json"
    if not path.exists():
        return {}
    try:
        pins_by_box = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    # Private name: the single source of port truth for event.conf.
    from quotient.setup import _SERVICE_TO_CHECK
    ports = {}
    for box_name, pins in pins_by_box.items():
        out = set()
        for pin in pins or []:
            if isinstance(pin, dict) and pin.get("plant_only"):
                continue
            name = pin if isinstance(pin, str) else pin.get("name")
            port = pin.get("port") if isinstance(pin, dict) else None
            if not port:
                mapped = _SERVICE_TO_CHECK.get(name)
                port = mapped[1].get("Port") if mapped else None
            if port:
                try:
                    out.add(int(port))
                except (TypeError, ValueError):
                    pass
        if out:
            ports[box_name] = sorted(out)
    return ports


def ports_closed_from_engine(ctx, ip, ports, per_port_timeout=3):
# The subset the engine cannot connect to — Quotient's own vantage, so an in-path firewall is
#     traversed exactly as scoring traverses it; an empty set means every scored port accepts.
    if not ports:
        return set()
    probe = "; ".join(
        f"timeout {per_port_timeout} bash -c 'echo > /dev/tcp/{ip}/{p}' 2>/dev/null "
        f"&& echo P{p}=OK || echo P{p}=CLOSED"
        for p in ports)
    try:
        r = ssh_on_gateway(ctx, probe, timeout=per_port_timeout * len(ports) + 20)
    except Exception:
        return set(ports)
    if r.returncode != 0:
        return set(ports)
    tokens = set((r.stdout or "").split())
    return {p for p in ports if f"P{p}=OK" not in tokens}


RESET_PROBE_ATTEMPTS = 3
RESET_PROBE_GAP_S = 20


def probe_box_health(ctx, t, ports, node):
# ('healthy'|'pam-trap'|'unhealthy', detail). The SSH leg exists because a tz-ready rollback
#     boots a post-plant disk: a box in the PAM planted-box trap dies at SSH preauth with its
#     ports still listening.
    windows = box_platform(t["box"]) == "windows"
    detail = "no attempt"
    for attempt in range(1, RESET_PROBE_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(RESET_PROBE_GAP_S)
        if windows:
            if not wait_for_guest_agent(node, t["vmid"], timeout=20):
                detail = "guest agent silent"
                continue
        else:
            klass = classify_ssh_failure(ctx, t["ip"], user=ctx.get("box_username", "ubuntu"))
            if klass == "pam-trap":
                return "pam-trap", ("SSH dies at the PAM account stage preauth — planted-box "
                                    "restart trap (docs/known-issues.md)")
            if klass != "ok":
                detail = f"ssh {klass}"
                continue
        missing = ports_closed_from_engine(ctx, t["ip"], ports)
        if not missing:
            return "healthy", "guest agent + scored ports" if windows else "ssh + scored ports"
        detail = "closed scored port(s): " + ", ".join(str(p) for p in sorted(missing))
    return "unhealthy", detail


def mode_reset(targets, rctx):
# Each rung gets only the boxes the previous rung left unhealthy; a PAM-trap verdict escalates
#     one rung rather than straight to rebuild, because the trap lives on the post-plant disk.
    ctx, node, comp_dir, boxes = rctx.ctx, rctx.node, rctx.comp_dir, rctx.boxes
    print(f"\n  Reset ladder: '{SNAP_READY}' rollback -> '{SNAP_BASE}' rollback + replant "
          f"-> golden rebuild. Escalating only the boxes each rung leaves unhealthy.")
    ports_by_box = scored_ports_for(comp_dir)
    levels = {}
    assets = []

    def nakon_assets():
        if not assets:
            assets.extend(prepare_nakon_assets(comp_dir, boxes))
        return assets

    def _rung(batch, run):
    # carry is nonempty only on a wholesale failure: the rung touched nobody, so the whole
    #         batch goes to the next rung unprobed.
        try:
            return run(batch) or [], []
        except (Exception, SystemExit) as e:
            print(f"    rung failed wholesale ({type(e).__name__}: {e}) — escalating the batch")
            return [], list(batch)

    def _split_by_snapshot(batch, snap):
        have, missing = [], []
        for t in batch:
            if snap in list_snapshots(t.get("node") or node, t["vmid"]):
                have.append(t)
            else:
                missing.append(t)
        return have, missing

    def _probe_all(batch, level_label):
        escalate = []
        for t in batch:
            verdict, detail = probe_box_health(ctx, t, ports_by_box.get(t["box_name"], ()), node)
            name = f"{t['team_key']}/{t['box_name']}"
            if verdict == "healthy":
                print(f"    {name}: healthy ({detail})")
                levels[t["vm_name"]] = level_label
            elif verdict == "pam-trap":
                print(f"    {name}: PAM PLANTED-BOX TRAP — {detail}")
                escalate.append(t)
            else:
                print(f"    {name}: unhealthy ({detail})")
                escalate.append(t)
        return escalate

    pending = list(targets)

    have_ready, pending = _split_by_snapshot(pending, SNAP_READY)
    for t in pending:
        print(f"    {t['team_key']}/{t['box_name']}: no '{SNAP_READY}' snapshot — starts at rung 2")
    if have_ready:
        print(f"\n  [reset 1/3] {len(have_ready)} box(es): rollback to '{SNAP_READY}'...")
        restored, carry = _rung(have_ready, lambda b: mode_rollback(
            b, rctx, SNAP_READY, None, None, reconfigure=False))
        pending += carry + _probe_all(restored, f"'{SNAP_READY}' rollback")

    have_base, pending = _split_by_snapshot(pending, SNAP_BASE)
    for t in pending:
        print(f"    {t['team_key']}/{t['box_name']}: no '{SNAP_BASE}' snapshot — starts at rebuild")
    if have_base:
        print(f"\n  [reset 2/3] {len(have_base)} box(es): rollback to '{SNAP_BASE}' + replant...")
        cfg, bundle = nakon_assets()
        restored, carry = _rung(have_base, lambda b: mode_rollback(
            b, rctx, SNAP_BASE, cfg, bundle, reconfigure=True))
        pending += carry + _probe_all(restored, f"'{SNAP_BASE}' rollback + replant")

    if pending:
        print(f"\n  [reset 3/3] {len(pending)} box(es): rebuild from golden template...")
        cfg, bundle = nakon_assets()
        rebuilt, carry = _rung(pending, lambda b: mode_rebuild(
            b, rctx, cfg, bundle))
        pending += carry + _probe_all(rebuilt, "golden rebuild")

    print(f"\n{'='*64}")
    print("  Reset summary")
    print(f"{'='*64}")
    still_broken = []
    fixed = []
    for t in targets:
        name = f"{t['team_key']}/{t['box_name']}"
        label = levels.get(t["vm_name"])
        if label:
            fixed.append(t)
            print(f"    {name}: fixed via {label}")
        else:
            still_broken.append(t)
            print(f"    {name}: STILL BROKEN (every rung failed — see the per-box lines above)")
    if still_broken:
        raise SystemExit(
            f"\n  {len(still_broken)} box(es) remain broken after the full reset ladder — "
            f"diagnose by hand (verify-competition.py, engine console) before re-running.")
    return fixed, levels
