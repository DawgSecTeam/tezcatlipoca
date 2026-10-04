"""Engine-template build identity stamp, template cleaning, and re-prepare from template."""

from engine_cmd_ops import _run_engine_cmd
from engine_bootstrap_ops import push_quotient_env


def clean_engine_for_template(ctx, vmid=None):
    """Strip every per-deploy / per-competition trace before qm template (M4 template
    point): containers + data volumes (no scoring DB, teams or injects baked), .env and
    event.conf secrets, machine-id, cloud-init state, SSH host keys. The apt-cacher-ng
    package cache is deliberately KEPT — a warm cache speeds up every later run.

    `vmid` is the engine-template BUILD VM. Pass it whenever it is known: the command
    below runs over SSH to a management IP that a second competition's engine can share,
    and this is the most destructive command in the pipeline. See
    assert_engine_build_identity()."""
    if vmid is not None:
        assert_engine_build_identity(ctx, vmid)
    _run_engine_cmd(ctx, (
        "cd /opt/quotient && sudo docker compose down -v --remove-orphans 2>/dev/null; "
        # ~1 GB of image build cache is dead weight once the images exist.
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


# Identity stamp for the engine-template build VM. The build VM is booted on the PLANNED
# engine management IP and the cleanup is delivered by SSH to that IP, so with two
# engines (or an engine and a build VM) up on one node, ARP flaps can land the cleanup
# on a LIVE foreign engine — observed: an engine lost /etc/ssh/ssh_host_* (every session
# reset at kex while the listener stayed up) plus its /opt/quotient/.env and containers
# (known-issues: clean_engine_for_template). The stamp is written when the build VM
# first accepts SSH and removed by the clean, so it exists only while a build is in
# progress and never gets baked into the template.
ENGINE_BUILD_STAMP = "/etc/tezcatlipoca-build-id"


def engine_build_identity(vmid):
    """The identity string a build VM carries. Keyed on the reserved vmid, which is
    deterministic (engine_vmid + ENGINE_TEMPLATE_VMID_OFFSET) and unique per slot."""
    return f"tezcatlipoca-engine-template/{vmid}"


def stamp_engine_build(ctx, vmid):
    """Mark the guest at ctx['scoring_engine_ip'] as this build's VM.

    Called as soon as the freshly cloned build VM accepts SSH, before any other work, so
    a later SSH that resolves to the wrong machine is detectable."""
    return _run_engine_cmd(
        ctx, f"echo '{engine_build_identity(vmid)}' | sudo tee {ENGINE_BUILD_STAMP} >/dev/null",
        timeout=60, step="stamp engine build")


def assert_engine_build_identity(ctx, vmid):
    """Refuse to touch the engine unless the guest at this address is THIS build's VM.

    A missing stamp is fatal, not a warning: it means either we are pointed at a machine
    this pipeline never stamped (a foreign engine, or an engine clone made from a
    template) or the build VM is not the one we think it is. Both are worse than
    stopping."""
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
            f"TF_VAR_engine_mgmt_ip, or stop the other range, then retry.")


# Grow / to the whole disk. Terraform resizes the engine's virtual disk (main.tf: 40 GB)
# but nothing grew the partition/PV/LV/filesystem, so the engine ran on the base image's
# 10 GB root — full within one Windows+Linux deploy (winad-testrun 2026-09-25: 100% used,
# apt-cacher-ng answering 500 to every box; Postgres would be next). Idempotent: growpart
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
    """Per-deploy steps on an engine cloned from the engine template (M4).

    The clone boots with a fresh identity and fresh host keys (baked by the template's
    clean step). .env goes down BEFORE compose up: the fresh postgres volume must
    initialize with this competition's credentials — up-then-rewrite would leave a
    volume initialized with an empty password. compose up on a template with no
    volumes creates exactly that: an empty scoring DB every run."""
    print("  Preparing engine from template (.env + fresh-volume compose up)...")
    _run_engine_cmd(ctx, _GROW_ROOT_CMD, check=False, timeout=120,
                    step="grow engine root filesystem")
    push_quotient_env(ctx, postgres_password, redis_password)
    _run_engine_cmd(ctx, "cd /opt/quotient && sudo docker compose up -d", timeout=600,
                    step="compose up")
    _run_engine_cmd(ctx, (
        "ss -ltn | grep -q ':3142 ' && echo '  apt-cacher-ng listening on 3142' "
        "|| sudo systemctl enable --now apt-cacher-ng"
    ), check=False, timeout=60, step="ensure apt-cacher-ng")
