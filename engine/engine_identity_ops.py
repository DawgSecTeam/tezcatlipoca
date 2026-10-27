"""Engine-template build identity stamp, template cleaning, and re-prepare from template."""

import time

from engine.engine_cmd_ops import _run_engine_cmd, start_quotient
from common.utils import dns_fix_cmd
from engine.engine_bootstrap_ops import _install_firewall_and_healthcheck, push_quotient_env


def clean_engine_for_template(ctx, vmid):
# The apt-cacher-ng package cache is deliberately KEPT — a warm cache speeds every later run.
    assert_engine_build_identity(ctx, vmid)
    _run_engine_cmd(ctx, (
        "cd /opt/quotient && sudo docker compose down -v --remove-orphans 2>/dev/null; "
        "sudo docker builder prune -af >/dev/null 2>&1; "
        "sudo rm -f /opt/quotient/.env /opt/quotient/config/event.conf && "
        "sudo rm -rf /opt/quotient/config/credlists && "
        "sudo truncate -s 0 /etc/machine-id && "
        "sudo rm -f /var/lib/dbus/machine-id && "
        "sudo cloud-init clean --logs --machine-id && "
        "sudo rm -f /etc/ssh/ssh_host_* && "
        # The stamp must NOT survive into the template, or every clone would carry it.
        f"sudo rm -f {ENGINE_BUILD_STAMP} && "
        "echo '    engine template cleaned (volumes, secrets, identity, host keys)'"
    ), timeout=300, step="clean engine for template")


# The build VM boots on the PLANNED engine mgmt IP and is cleaned over SSH to it: with two
# engines up on one node, ARP flaps can land the cleanup on a LIVE foreign engine (observed:
# lost host keys, .env and containers). Written on first SSH, removed by the clean, so it
# never gets baked into the template.
ENGINE_BUILD_STAMP = "/etc/tezcatlipoca-build-id"


def engine_build_identity(vmid):
    return f"tezcatlipoca-engine-template/{vmid}"


def stamp_engine_build(ctx, vmid):
    return _run_engine_cmd(
        ctx, f"echo '{engine_build_identity(vmid)}' | sudo tee {ENGINE_BUILD_STAMP} >/dev/null",
        timeout=60, step="stamp engine build")


def assert_engine_build_identity(ctx, vmid):
    expected = engine_build_identity(vmid)
    result = _run_engine_cmd(ctx, f"cat {ENGINE_BUILD_STAMP} 2>/dev/null || true",
                             timeout=60, capture=True, step="verify engine build identity")
    found = (getattr(result, "stdout", "") or "").strip()
    if found != expected:
        raise RuntimeError(
            f"refusing to run a destructive engine step: the host at "
            f"{ctx.get('scoring_engine_ip')!r} reports build identity {found!r}, expected "
            f"{expected!r}. This is the shared-management-IP hazard — the IP is answering "
            f"for a different machine (a foreign competition's live engine, or a clone of "
            f"a template). Nothing was changed. Give this competition its own "
            f"TEZ_ENGINE_MGMT_IP, or stop the other range, then retry.")


def prepare_engine_from_template(ctx, postgres_password, redis_password):
# .env goes down BEFORE compose up: the fresh postgres volume must initialize with this
# competition's credentials — up-then-rewrite leaves it initialized with an empty password.
    print("  Preparing engine from template (.env + fresh-volume compose up)...")
    from guests.root_disk_ops import grow_root_command
    _run_engine_cmd(ctx, grow_root_command(), check=False, timeout=120,
                    step="grow engine root filesystem")
    # Same baked-in 8.8.8.8 as the build path: the clone inherits the image's resolved drop-in,
    # so the placement list must be written before anything resolves a name.
    _run_engine_cmd(ctx, dns_fix_cmd(), timeout=60, step="fix engine DNS")
    push_quotient_env(ctx, postgres_password, redis_password)
    start_quotient(ctx)
    _run_engine_cmd(ctx, (
        "ss -ltn | grep -q ':3142 ' && echo '  apt-cacher-ng listening on 3142' "
        "|| sudo systemctl enable --now apt-cacher-ng"
    ), check=False, timeout=60, step="ensure apt-cacher-ng")
    # Kernel iptables state never survives a clone: a template-derived engine relies on the
    # template's range-firewall.timer, which fires on boot only if the template build reached
    # that step (a build that failed earlier leaves no units — observed on the leftover build
    # VM, run-62f9e120). Installing here makes the team NAT immediate and independent of the
    # template's build state, which is what the golden builds on the team bridges depend on.
    _install_firewall_and_healthcheck(ctx)
