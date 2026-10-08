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

from utils import compfile_flag, compfile_value, record_degradation

REPO = Path(__file__).resolve().parent
BAD_AUTO = REPO.parent / "bad-auto"

DEFAULT_RED_IP = "10.0.0.199"
RED_USER = "sysadmin"
SEED_DEPTH_MAX = 3
# Where `badauto deploy` stages the package on red01 (the systemd unit's
# WorkingDirectory — the package is imported by cwd, never pip-installed).
INSTALL_DIR = "/opt/bad-auto"


def _bad_auto_present():
    return (BAD_AUTO / "badauto" / "__main__.py").exists()


def _configured_red_ip():
    """red01's address from bad-auto's config.yaml, when a sibling checkout exists."""
    cfg_path = BAD_AUTO / "config.yaml"
    if not cfg_path.exists():
        return None
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    return ((cfg.get("deploy") or {}).get("red_ip")) or None


def red_ip_for(comp_dir):
    """Compfile override -> bad-auto config -> built-in default."""
    return (compfile_value(comp_dir / "Compfile", "assume_breach_red_ip", "")
            or _configured_red_ip() or DEFAULT_RED_IP)


def _deploy_red(comp_dir, env, timeout=1800):
    """`badauto deploy` (no --start): red01 + the realm engine DNAT, no director."""
    return subprocess.run(
        ["python3", "-m", "badauto", "deploy", "--competition", str(comp_dir)],
        cwd=str(BAD_AUTO), env=env, capture_output=True, text=True, timeout=timeout)


def destroy_red(comp_dir, env=None, timeout=900):
    """`badauto destroy`: red01 + its realm NAT rules.

    red01 is bad-auto's VM, not a terraform-managed box, so destroying the range
    leaves it running — with its LLM key, its beacon tasking and its DNAT rules.
    That is the scale8 soak leak (red01 998 alive after the driver printed DONE).
    The harness destroys it in its own teardown; this is the step the standalone
    teardown was missing for the run whose harness died."""
    # BAuto_COMPETITION_DIR must be set: bad-auto's destroy verifies the VM's deploy
    # stamp against the competition directory, and without it the identity guard
    # refuses the target ("mismatched target is refused") — caught live 2026-10-08,
    # where destroy-competition's env reached the call without it.
    env = {**(env if env is not None else os.environ), "BAuto_COMPETITION_DIR": str(comp_dir)}
    return subprocess.run(
        ["python3", "-m", "badauto", "destroy", "--competition", str(comp_dir), "--yes"],
        cwd=str(BAD_AUTO), env=env, capture_output=True, text=True, timeout=timeout)


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
    red_ip = red_ip_for(comp_dir)
    ssh_key = getattr(ctx, "ssh_key_abs", None) or getattr(ctx, "ssh_key", None)
    if not ssh_key:
        record_degradation("assume_breach", "no ssh key on the deploy context")
        print("  assume-breach: no ssh key available — skipped")
        return {"ok": False, "reason": "no ssh key"}

    env = {**os.environ, "BAuto_COMPETITION_DIR": str(comp_dir)}
    print(f"  assume-breach: deploying red01 + realm engine DNAT (bad-auto)...")
    try:
        deployed = _deploy_red(comp_dir, env)
    except subprocess.TimeoutExpired:
        record_degradation("assume_breach_deploy", "badauto deploy timed out")
        print("  assume-breach: bad-auto deploy timed out — skipped")
        return {"ok": False, "reason": "deploy timeout"}
    if deployed.returncode != 0:
        tail = ((deployed.stderr or "") + (deployed.stdout or ""))[-300:]
        record_degradation("assume_breach_deploy", tail)
        print("  assume-breach: bad-auto deploy FAILED (see degradations)")
        return {"ok": False, "reason": "deploy failed"}

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
        return {"ok": False, "reason": "seed failed"}

    summary = {"ok": True, "red_ip": red_ip, "depth": depth, "at": _now()}
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
