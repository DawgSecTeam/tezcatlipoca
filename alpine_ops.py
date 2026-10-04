"""Alpine apk/OpenRC service shim (the catalog scripts are apt/dnf/yum only)."""

import json
import os
import shlex
import time

from range_ops import guest_agent_exec_root, wait_for_guest_agent
from ssh_ops import ssh_via_gateway
from utils import PRINT_LOCK, run_concurrent


# Pinned scored service -> (apk package, OpenRC service) for the alpine_services shim
# (ensure_alpine_services). The vulndb catalog service scripts are apt/dnf/yum-only, so
# Alpine boxes get their services here instead (distro-matrix-2026-09-27).
ALPINE_SERVICES = {
    "nginx": ("nginx", "nginx"),
    "apache": ("apache2", "apache2"),
    "bind": ("bind", "named"),
    # sshd ships in the -fix template (nakon's own transport); the shim entry just
    # re-asserts it so an `ssh` pin scores a credlist Ssh check on Alpine boxes.
    "ssh": ("openssh", "sshd"),
}


# Alpine's nginx package ships a stub vhost that returns 404 for everything — the
# Quotient web check needs 200 on /. This conf (base64 to survive the ssh quoting
# layers) replaces the stub.
_ALPINE_NGINX_200_VHOST_B64 = ("c2VydmVyIHsKICAgIGxpc3RlbiA4MCBkZWZhdWx0X3NlcnZlcjsKICAgIGxpc3Rl"
                               "biBbOjpdOjgwIGRlZmF1bHRfc2VydmVyOwogICAgbG9jYXRpb24gLyB7IHJldHVy"
                               "biAyMDAgInR6LWFscGluZSB1cFxuIjsgYWRkX2hlYWRlciBDb250ZW50LVR5cGUg"
                               "dGV4dC9wbGFpbjsgfQp9Cg==")


def ensure_alpine_services(comp_dir, targets, ctx):
    """Install + enable pinned scored services on Alpine boxes via apk + OpenRC.

    The vulndb service configs only speak apt/dnf/yum (and systemd), so on Alpine every
    catalog service step exits 1 in 0 seconds (distro-matrix-2026-09-27). With the
    Compfile `alpine_services` knob set, the golden plant runs non-strict and this shim
    owns Alpine services instead: apk add, rc-update add default, rc-service start, and
    a status probe as the pass condition. Idempotent — clones inherit the golden disk,
    so phase 5's pass usually confirms rather than installs. Unknown pinned services
    are reported, not silently skipped."""
    box_username = ctx.get("box_username", "ubuntu")
    pins = json.loads((comp_dir / "box_services.json").read_text())
    alpine = [t for t in targets
              if "alpine" in str(t.get("box", {}).get("template") or t.get("template", "")).lower()]
    if not alpine:
        return

    print("  Ensuring pinned services on Alpine boxes (apk + OpenRC shim)...")
    pending = []
    for t in alpine:
        box_name = t.get("box_name") or t["box"]["name"]
        pin_names = [s if isinstance(s, str) else s.get("name") for s in pins.get(box_name, [])]
        unknown = [s for s in pin_names if s not in ALPINE_SERVICES]
        if unknown:
            raise RuntimeError(
                f"alpine_services shim has no apk mapping for {unknown} pinned on {box_name} — "
                f"add it to hardening_ops.ALPINE_SERVICES or drop the pin")
        wanted = [ALPINE_SERVICES[s] for s in pin_names]
        if wanted:
            pending.append((t, wanted))

    def _ensure(t_wanted):
        t, wanted = t_wanted
        ip = t["ip"]
        markers = " ".join(f"TZ-SVC-OK-{pkg}" for pkg, _svc in wanted)
        blocks = []
        for pkg, svc in wanted:
            block = (f"echo \"TZ-SVC-TRY-{pkg}\"; apk add --no-cache {pkg} && rc-update add {svc} default && "
                     f"{{ rc-service {svc} restart >/dev/null 2>&1 || rc-service {svc} start; }} && ")
            if pkg == "nginx":
                block += (f"echo {_ALPINE_NGINX_200_VHOST_B64} | base64 -d > /etc/nginx/http.d/tz-default.conf && "
                          "rm -f /etc/nginx/http.d/default.conf && ")
            block += f"rc-service {svc} status >/dev/null && echo \"TZ-SVC-OK-{pkg}\""
            blocks.append(block)
        script = "; ".join(blocks)

        def _markers_ok(stdout):
            missing = [m for m in markers.split() if m not in (stdout or "")]
            return missing

        # Primary channel: guest agent as root. The repair sweep's writable-sudoers
        # pin makes sudoers.d world-writable post-clone, sudo then ignores the whole
        # dir and `sudo sh -c` as medic demands a password it can't answer
        # (live-found 2026-09-28). The -fix alpine template ships the agent.
        node = os.environ["TF_VAR_proxmox_node"]
        vmid = t.get("vmid")
        if vmid is not None:
            try:
                wait_for_guest_agent(node, vmid, timeout=120)
                rc, out, err = guest_agent_exec_root(node, vmid, script, timeout=180)
                missing = _markers_ok(out)
                if rc == 0 and not missing:
                    with PRINT_LOCK:
                        print(f"    Alpine services ensured on {ip} ({', '.join(p for p, _ in wanted)}) [guest-agent]")
                    return True
                with PRINT_LOCK:
                    print(f"    Alpine shim guest-agent attempt failed for {ip} "
                          f"(rc={rc}, missing={missing}) {(err or '').strip()[:160]}")
            except Exception as exc:
                with PRINT_LOCK:
                    print(f"    Alpine shim guest-agent channel failed for {ip} (vmid {vmid}): {exc} — falling back to ssh")
        for attempt in range(1, 5):
            try:
                r = ssh_via_gateway(ctx, ip, f"sudo sh -c {shlex.quote(script)}",
                                    timeout=180, user=box_username)
                missing = _markers_ok(r.stdout)
                if r.returncode == 0 and not missing:
                    with PRINT_LOCK:
                        print(f"    Alpine services ensured on {ip} ({', '.join(p for p, _ in wanted)})")
                    return True
                err = (r.stderr or "").strip()[:160]
                with PRINT_LOCK:
                    print(f"    Alpine shim attempt {attempt}/4 failed for {ip} "
                          f"(rc={r.returncode}, missing={missing}) {err}")
            except Exception as exc:
                with PRINT_LOCK:
                    print(f"    Alpine shim attempt {attempt}/4 failed for {ip}: {exc}")
            time.sleep(10)
        return False

    results = run_concurrent(pending, _ensure)
    failed = sorted({t["ip"] for (t, _w), r in zip(pending, results) if r is not True})
    if failed:
        raise RuntimeError(f"Alpine service shim failed on {failed} — nginx/scores would be DOWN")
