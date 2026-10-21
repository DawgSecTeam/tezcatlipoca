"""Sliver C2 on the competition's C2 box — server install, operator profile, listeners and the transport port map the engine DNATs."""

import base64
import contextlib
import fcntl
import os
import re
import socket
import subprocess
from pathlib import Path

from common.utils import compfile_value, record_degradation

SLIVER_VERSION = "v1.7.8"
SLIVER_REPO = "https://github.com/BishopFox/sliver"
SERVER_NAME = "sliver-server_linux-amd64"
INSTALL_DIR = "/opt/sliver"
SERVER_BIN = f"{INSTALL_DIR}/sliver-server"
OPERATOR_CONFIG = "/etc/tlaloc/sliver.cfg"
DAEMON_PORT = 31337
# v1.7.8 has no QUIC transport; these are all of them.
TRANSPORTS = ("mtls", "http", "https", "dns", "wg", "named-pipe", "tcp-pivot")
# gateway port -> server port. DNS is the odd one: the implant resolver is
# pinned at 53, so the gateway takes 53 and the server keeps 5353.
PORTS = {
    "mtls": {"gateway": 8888, "server": 8888, "proto": "tcp"},
    "http": {"gateway": 8080, "server": 8080, "proto": "tcp"},
    "https": {"gateway": 8443, "server": 8443, "proto": "tcp"},
    "dns": {"gateway": 53, "server": 5353, "proto": "udp"},
    "wg": {"gateway": 51820, "server": 51820, "proto": "udp"},
}
DEFAULT_DNS_DOMAIN = "c2.sliver.range"
INSTALL_TIMEOUT = 2400
SHORT_TIMEOUT = 300


def sliver_enabled(comp_dir):
    raw = compfile_value(Path(comp_dir) / "Compfile", "red.c2") or ""
    engine = (compfile_value(Path(comp_dir) / "Compfile", "red.c2_engine")
              or "sliver").strip().lower()
    return engine == "sliver" and str(raw).strip().lower() not in (
        "off", "false", "0", "no", "mythic")


def c2_box_for(comp_dir, default_ip=None):
    raw = compfile_value(Path(comp_dir) / "Compfile", "red.c2_ip", "") or ""
    return str(raw).strip() or default_ip


def transport_ports():
    return {name: dict(value) for name, value in PORTS.items()}


def dns_domain_for(comp_dir, comp_id=None):
    raw = compfile_value(Path(comp_dir) / "Compfile", "red.c2_dns_domain", "") or ""
    if str(raw).strip():
        return str(raw).strip().strip(".").lower()
    name = comp_id or Path(comp_dir).resolve().name
    safe = re.sub(r"[^a-z0-9-]", "-", str(name).lower()).strip("-") or "range"
    return f"c2.{safe}.range"


def c2_plan(comp_dir, *, red_ip=None, comp_id=None):
    comp_id = comp_id or Path(comp_dir).resolve().name
    box = c2_box_for(comp_dir, default_ip=red_ip)
    ports = transport_ports()
    return {"box": box, "operation": comp_id,
            "domain": dns_domain_for(comp_dir, comp_id),
            "ports": ports, "daemon_port": DAEMON_PORT}


def sliver_c2_block(comp_dir, *, server, operation, domain, ports,
                    spread=None, callback_wait=None):
    block = {"engine": "sliver", "enabled": True, "operation": operation,
             "base_url": f"https://{server}:{DAEMON_PORT}",
             "operator_config": OPERATOR_CONFIG, "dns_domain": domain,
             "transports": ports}
    if spread:
        block["implant_spread"] = spread
    if callback_wait:
        block["callback_wait_sec"] = int(callback_wait)
    return block


def _run(ssh_key, box_ip, script, *, runner=None, timeout=SHORT_TIMEOUT):
    blob = base64.b64encode(script.encode()).decode()
    argv = ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=15",
            f"sysadmin@{box_ip}", f"echo {blob} | base64 -d | sudo -n bash"]
    runner = runner or _default_runner
    return runner(argv, timeout=timeout)


def install_script(box_ip):
    url = f"{SLIVER_REPO}/releases/download/{SLIVER_VERSION}/{SERVER_NAME}"
    return f"""set -e
mkdir -p {INSTALL_DIR} /etc/tlaloc
# Build scratch from a killed c2-prep survives and makes the next Generate fail
# with "rename import dir: target exists"; the Go build cache is elsewhere.
rm -rf /root/.sliver/slivers
if [ ! -x {SERVER_BIN} ]; then
  curl -fsSL --retry 3 --retry-delay 5 -o {SERVER_BIN}.tmp {url}
  chmod 755 {SERVER_BIN}.tmp
  mv {SERVER_BIN}.tmp {SERVER_BIN}
fi
cat > /etc/systemd/system/sliver.service <<'EOF'
[Unit]
Description=Sliver C2 server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/sliver
ExecStart=/opt/sliver/sliver-server daemon --lhost {box_ip} --lport {DAEMON_PORT}
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable sliver.service
systemctl restart sliver.service
for _ in $(seq 1 300); do
  if ss -ltn | grep -q ':{DAEMON_PORT} '; then exit 0; fi
  sleep 2
done
journalctl -u sliver.service -n 30 --no-pager >&2 || true
exit 1
"""


def operator_script(box_ip):
    return f"""set -e
rm -f {OPERATOR_CONFIG}
{SERVER_BIN} operator --name tlaloc --lhost {box_ip} --lport {DAEMON_PORT} \
  --permissions all --save {OPERATOR_CONFIG} >/dev/null
chmod 600 {OPERATOR_CONFIG}
test -s {OPERATOR_CONFIG}
"""


def prepare_sliver(comp_dir, *, box_ip, ssh_user, ssh_key, runner=None,
                   plan=None, log=print):
    comp_dir = Path(comp_dir)
    runner = runner or _default_runner
    plan = plan or c2_plan(comp_dir, red_ip=box_ip)
    result = {"ok": False, "box": str(box_ip), "engine": "sliver",
              "version": SLIVER_VERSION, "reached": [], "degradations": [],
              "c2": None, "environment": {"sshd": True, "sliver": False},
              "server": SERVER_BIN, "operator_config": OPERATOR_CONFIG,
              "ports": plan["ports"], "domain": plan["domain"]}
    if not box_ip:
        result["degradations"].append("no C2 box: red.c2_ip unset and red01 unknown")
        return result

    with sliver_lock(box_ip):
        proc = _run(ssh_key, box_ip, install_script(box_ip), runner=runner,
                    timeout=INSTALL_TIMEOUT)
        result["reached"].append("install")
        result["install_output"] = _tail(proc, 1200)
        if getattr(proc, "returncode", 1) != 0:
            result["degradations"].append(f"sliver server install: {_tail(proc)}")
            return result

        proc = _run(ssh_key, box_ip, operator_script(box_ip), runner=runner,
                    timeout=SHORT_TIMEOUT)
        result["reached"].append("operator")
        result["operator_output"] = _tail(proc, 600)
        if getattr(proc, "returncode", 1) != 0:
            result["degradations"].append(f"sliver operator config: {_tail(proc)}")
            return result

    spread = compfile_value(comp_dir / "Compfile", "red.c2_implant_spread", "") or ""
    wait = compfile_value(comp_dir / "Compfile", "red.c2_callback_wait_sec", "") or ""
    result["c2"] = sliver_c2_block(comp_dir, server=box_ip,
                                   operation=plan["operation"],
                                   domain=plan["domain"], ports=plan["ports"],
                                   spread=str(spread).strip() or None,
                                   callback_wait=str(wait).strip() or None)
    result["environment"] = {"sshd": True, "sliver": True}
    result["ok"] = not result["degradations"]
    if log:
        log(f"  sliver      : {'ok' if result['ok'] else 'DEGRADED'} on {box_ip} "
            f"— reached {', '.join(result['reached']) or 'nothing'}, "
            f"transport ports {', '.join(str(p['gateway']) for p in plan['ports'].values())}")
    return result


def apply_c2_to_config(config_path, c2_block):
    """The deploy renders the config first and pushes later, so the C2 step must merge into that same file."""
    import yaml
    path = Path(config_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    section = data.setdefault("c2", {})
    if not isinstance(section, dict):
        section = {}
        data["c2"] = section
    section.update(c2_block or {})
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def sliver_answering(box_ip, timeout=3):
    try:
        with socket.create_connection((str(box_ip), DAEMON_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def sliver_lock_path(box_ip):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(box_ip))
    return Path.home() / ".tezcatlipoca" / "locks" / f"sliver-{safe}.lock"


@contextlib.contextmanager
def sliver_lock(box_ip):
    """Serialize one C2 box's install/configure sequence across processes."""
    path = sliver_lock_path(box_ip)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def degrade(result, where):
    for item in result.get("degradations") or []:
        record_degradation(f"sliver_{where}", str(item)[:400])


def _tail(proc, n=300):
    return ((getattr(proc, "stdout", "") or "")
            + (getattr(proc, "stderr", "") or ""))[-n:]


def _default_runner(argv, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    if "env" not in kwargs:
        kwargs["env"] = {k: v for k, v in os.environ.items()
                         if k not in _STRIPPED_ENV}
    return subprocess.run(argv, **kwargs)


# Sliver is third-party: never leak the Proxmox token or dashboard bearers into it
_STRIPPED_ENV = ("TEZ_PROXMOX_API_TOKEN", "TEZ_OPS_TOKEN", "TEZ_DASHBOARD_TOKENS")
