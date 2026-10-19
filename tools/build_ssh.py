"""SSH transport into a template builder's scratch VM.

Three modes, from `--mode`/`--operator-iface`/`--bootstrap-ip` or
`TEZ_BUILD_MODE`/`TEZ_BUILD_OPERATOR_IFACE`/`TEZ_BUILD_BOOTSTRAP_IP`:

  node      alias 192.168.1.2 on the node's bridge and hop through root@<node>
  operator  alias 192.168.1.2 on a local NIC sharing the scratch bridge's L2
  direct    SSH straight to an operator-routable bootstrap address, no alias

The guest keeps the deploy-contract LAN 192.168.1.1/24 in node and operator mode. Direct
mode puts the bootstrap address there instead; the builders restore the contract at the
console before sealing so sealed templates still match phase 5.
"""

import argparse
import subprocess
import sys
from contextlib import contextmanager

from estate import config

ALIAS_IP = "192.168.1.2"
CONTRACT_IP = "192.168.1.1"
MODES = ("node", "operator", "direct")


def options(argv=None):
    """Resolve the transport from flags after the command, then TEZ_BUILD_* config."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", default=config.active().knobs.build_mode or "node")
    parser.add_argument("--operator-iface",
                        default=config.active().knobs.build_operator_iface)
    parser.add_argument("--bootstrap-ip", default=config.active().knobs.build_bootstrap_ip)
    opts, _ = parser.parse_known_args(sys.argv[2:] if argv is None else argv)
    if opts.mode not in MODES:
        raise SystemExit(f"--mode must be one of {', '.join(MODES)}, got {opts.mode!r}")
    if opts.mode == "operator" and not opts.operator_iface:
        raise SystemExit("--mode operator needs --operator-iface or "
                         "TEZ_BUILD_OPERATOR_IFACE")
    if opts.mode == "direct" and not opts.bootstrap_ip:
        raise SystemExit("--mode direct needs --bootstrap-ip or TEZ_BUILD_BOOTSTRAP_IP")
    return opts


def guest_ip(opts):
    """The address the builder SSHes to; direct mode uses the bootstrap address."""
    return opts.bootstrap_ip if opts.mode == "direct" else CONTRACT_IP


def guest_argv(opts, *, node, key, user, command=None, extra=()):
    argv = ["ssh", "-i", str(key), "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            *extra]
    if opts.mode == "node":
        proxy = f"ssh -i {key} -o BatchMode=yes -W %h:%p root@{node}"
        argv.append(f"-o ProxyCommand={proxy}")
    argv.append(f"{user}@{guest_ip(opts)}")
    if command is not None:
        argv.append(command)
    return argv


def _node_ssh(node, key, command, timeout=30):
    return subprocess.run(
        ["ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         f"root@{node}", command],
        capture_output=True, text=True, timeout=timeout)


def _local_alias(verb, iface):
    result = subprocess.run(
        ["sudo", "-n", "ip", "addr", verb, f"{ALIAS_IP}/24", "dev", iface],
        capture_output=True, text=True, timeout=15)
    if result.returncode and "File exists" not in (result.stderr or "") \
            and "Cannot assign requested address" not in (result.stderr or ""):
        raise SystemExit(f"local alias {verb} on {iface} failed: "
                         f"{(result.stderr or '').strip()[:200]}")


@contextmanager
def aliased(opts, *, node, bridge, key):
    """Hold the hop alias for the body; direct mode never aliases."""
    if opts.mode == "node":
        _node_ssh(node, key, f"ip addr add {ALIAS_IP}/24 dev {bridge} 2>/dev/null || true")
    elif opts.mode == "operator":
        _local_alias("add", opts.operator_iface)
    try:
        yield
    finally:
        if opts.mode == "node":
            _node_ssh(node, key, f"ip addr del {ALIAS_IP}/24 dev {bridge} 2>/dev/null || true")
        elif opts.mode == "operator":
            _local_alias("del", opts.operator_iface)
