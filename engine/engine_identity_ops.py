"""Engine-template build identity stamp, template cleaning, and re-prepare from template."""

import time

from engine.engine_cmd_ops import _run_engine_cmd
from engine.engine_bootstrap_ops import push_quotient_env


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


# The create path sizes the engine disk 40 GB but nothing grew the partition/PV/LV/fs, so
# the engine ran on the base image's 10 GB root and filled it within one deploy. growpart
# exits nonzero ("NOCHANGE") once the partition already fills the disk.
_GROW_ROOT_CMD = (
    "ROOT=$(findmnt -no SOURCE /); "
    "if sudo lvs \"$ROOT\" >/dev/null 2>&1; then "
    "PV=$(sudo pvs --noheadings -o pv_name | head -1 | xargs); "
    "DISK=/dev/$(lsblk -no pkname \"$PV\" | head -1); "
    "PART=$(cat /sys/class/block/$(basename \"$PV\")/partition); "
    "sudo growpart \"$DISK\" \"$PART\" >/dev/null; "
    "sudo pvresize \"$PV\" >/dev/null && sudo lvextend -r -l +100%FREE \"$ROOT\" >/dev/null 2>&1; "
    "else "
    "DISK=/dev/$(lsblk -no pkname \"$ROOT\" | head -1); "
    "PART=$(cat /sys/class/block/$(basename \"$ROOT\")/partition); "
    "sudo growpart \"$DISK\" \"$PART\" >/dev/null && sudo resize2fs \"$ROOT\" >/dev/null 2>&1; "
    "fi; "
    "echo \"  engine root: $(df -h / | awk 'NR==2 {print $2\" total, \"$4\" free\"}')\""
)


def prepare_engine_from_template(ctx, postgres_password, redis_password):
# .env goes down BEFORE compose up: the fresh postgres volume must initialize with this
# competition's credentials — up-then-rewrite leaves it initialized with an empty password.
    print("  Preparing engine from template (.env + fresh-volume compose up)...")
    _run_engine_cmd(ctx, _GROW_ROOT_CMD, check=False, timeout=120,
                    step="grow engine root filesystem")
    push_quotient_env(ctx, postgres_password, redis_password)
    # Same race as the build path (bootstrap_scoring_engine): a fresh volume's Postgres can
    # miss compose's health window and the server exits 2 once, so one failure is not a dead
    # engine (live: run-e5fe9b87 failed here; the same command succeeded a minute later).
    last = None
    for attempt in range(3):
        try:
            _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose up -d",
                            timeout=600, step="compose up")
            last = None
            break
        except Exception as e:  # noqa: BLE001 - the DB health window is a race
            last = e
            print(f"    compose up attempt {attempt + 1} failed ({e}); retrying")
            time.sleep(15)
    if last is not None:
        raise last
    _run_engine_cmd(ctx, (
        "ss -ltn | grep -q ':3142 ' && echo '  apt-cacher-ng listening on 3142' "
        "|| sudo systemctl enable --now apt-cacher-ng"
    ), check=False, timeout=60, step="ensure apt-cacher-ng")
