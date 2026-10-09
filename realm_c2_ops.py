"""Provision the Realm C2 (tavern) on the redteam server the deploy provisions.

Historically the C2 has been the STATIC VM101 (`malware-dev`, 10.0.0.117): every
range's beacons DNAT through the scoring engine to that one long-lived box, which
couples every competition to VM101 being alive and keeps the C2 outside the
range's own lifecycle. This module moves the C2 ONTO red01 — the redteam server
`badauto deploy` clones for the assume-breach plant — so each range carries its
own tavern with every beacon transport (grpc, http1, dns, icmp, quic) and the MCP
server enabled, and the whole thing dies with the range at teardown.

Source of truth is upstream: https://github.com/spellshift/realm.git — cloned
shallow onto red01 and built with `go build -o tavern_updated ./tavern/`. The
repo ships the prebuilt `tavern/internal/www/build` UI, so no node toolchain is
needed; apt's golang is too old (realm needs >= 1.26.2), so the toolchain comes
from the official go.dev tarball. The unit layout, the MariaDB durability pair
(MYSQL_ADDR + SECRETS_FILE_PATH — tavern otherwise wipes its state on every
restart and rotates its server key, orphaning every baked beacon), the
setcap/sysctl requirements of the icmp redirector and the port map mirror
VM101's live 2026-10-07 setup, which is the verified reference.

Gating (Compfile; all defaults shown):
    assume_breach 1             # red01 must exist for the C2 to live on it
    realm_c2_local 1            # ON by default — `0` keeps the legacy C2-on-VM101
    realm_c2_repo <url>         # https://github.com/spellshift/realm.git
    realm_c2_implant_host <ip>  # 10.0.0.117 (VM101 builds the imix implants)
    realm_c2_go_version <v>     # 1.26.9

The harness path (run-agent-scrim) uses the same provisioner when the operator's
bad-auto config sets `realm.c2_local: true`.

Implants: tavern is built from source, but the imix BEACONS are prebuilt Rust
artifacts (a full Rust toolchain on red01 is not worth the provision time).
They are relayed here from the implant host (VM101) straight into bad-auto's
staging paths on red01 (`/opt/bad-auto/realm/imix` and `imix-windows.exe`), the
paths `realm_plant` uploads from. This replaces bad-auto's own `_stage_realm`,
whose scp source is `realm.c2_ip` and therefore cannot work once the C2 IS
red01 (its failed attempt prints a harmless warning during the deploy).

Failure contract: warn-and-continue with `record_degradation`, like the rest of
the assume-breach step — a provisioning problem must not abort a deploy whose
range is otherwise healthy. The harness path raises instead (it runs pre-T0).
Teardown needs nothing extra: tavern lives on red01, which `badauto destroy`
already removes.
"""

import base64
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent
BAD_AUTO = REPO.parent / "bad-auto"

DEFAULT_REPO = "https://github.com/spellshift/realm.git"
DEFAULT_GO_VERSION = "1.26.9"
DEFAULT_IMPLANT_HOST = "10.0.0.117"      # VM101 malware-dev — the beacon-builder VM
DEFAULT_DNS_DOMAIN = "c2.dawgsec.range"
DEFAULT_C2_PORT = 8000
RED_USER = "sysadmin"
INSTALL_DIR = "/opt/bad-auto/realm"      # where bad-auto's realm_plant reads implants
# The tavern unit's MYSQL_PASSWD cannot be interpolated in the heredoc (a quoted
# 'UNIT' delimiter blocks expansion, and systemd does not expand variables it
# never defined), so the script writes this placeholder and sed-swaps the real
# password in — the DB user is created with the same $DBPW earlier in the script.
DBPW_PLACEHOLDER = "__REALM_TAVERN_DB_PW__"

# Prebuilt implant artifacts on the implant host (VM101) -> bad-auto's staged
# names on red01. WIN_IMPLANT_DST is bad-auto's WIN_PAYLOAD_NAME.
LINUX_IMPLANT_SRC = "realm/implants/target/release/imix"
WIN_IMPLANT_SRC_DEFAULT = "realm/beacons/imix-windows-multi.exe"
WIN_IMPLANT_DST = "imix-windows.exe"

# All callback transports. tcp_bind is a local chaining transport with no
# server-side listener; grpc needs nothing beyond the tavern core listener.
TRANSPORT_ORDER = ("grpc", "http1", "dns", "icmp", "quic")
# C2-side redirector listen ports (the DSNs aim at the ENGINE gw_port; the
# engine DNATs to these).
REDIRECTOR_PORTS = {"http1": 8001, "dns": 5300, "quic": 8443}
REDIRECTOR_UNIT = {
    "http1": "tavern-http1-redirector.service",
    "dns": "tavern-dns-redirector.service",
    "quic": "tavern-quic-redirector.service",
    "icmp": "tavern-icmp-redirector.service",
}
_REDIRECTOR_DESC = {
    "http1": "Realm C2 HTTP/1.1 redirector (TCP/{port} -> grpc upstream)",
    "dns": "Realm C2 DNS redirector ({domain})",
    "quic": "Realm C2 QUIC redirector (UDP/{port}, self-signed TLS)",
    "icmp": "Realm C2 ICMP redirector (raw ICMP echo transport)",
}
_REDIRECTOR_CMD = {
    "http1": "redirector --transport http1 --listen 0.0.0.0:{port} http://localhost:{c2_port}",
    "dns": ("redirector --transport dns "
            "--listen 0.0.0.0:{port}?domain={domain} http://localhost:{c2_port}"),
    "quic": "redirector --transport quic --listen 0.0.0.0:{port} http://localhost:{c2_port}",
    "icmp": "redirector --transport icmp --listen 0.0.0.0 http://localhost:{c2_port}",
}
_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.-]+$")
def _ssh_base(ssh_key):
    return ["-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10"]


# ---------------------------------------------------------------- knobs/config

def compfile_knobs(comp_dir):
    """Realm-C2 knobs from the Compfile, with defaults."""
    comp = Path(comp_dir) / "Compfile"
    from utils import compfile_flag, compfile_value  # local: utils imports heavy deps
    return {
        "enabled": bool(compfile_flag(comp, "realm_c2_local", 1)),
        "repo": compfile_value(comp, "realm_c2_repo", DEFAULT_REPO),
        "implant_host": compfile_value(comp, "realm_c2_implant_host", DEFAULT_IMPLANT_HOST),
        "go_version": compfile_value(comp, "realm_c2_go_version", DEFAULT_GO_VERSION),
    }


_ENGINE_URL_RE = re.compile(r"https?://(\d+\.\d+\.\d+\.\d+)")


def engine_ip_for(comp_dir):
    """The scoring engine's mgmt IP, or None.

    Source order matches the consumer: the competition's `credentials.txt` is where
    tezcatlipoca publishes the scoreboard URL (`Scoreboard: http://<engine>`) and is
    what `badauto deploy` reads for its DNAT, with the deploy environment's
    `TF_VAR_engine_mgmt_ip` as the fallback.
    """
    try:
        text = (Path(comp_dir) / "credentials.txt").read_text(encoding="utf-8")
    except OSError:
        text = ""
    m = _ENGINE_URL_RE.search(text)
    if m:
        return m.group(1)
    return os.environ.get("TF_VAR_engine_mgmt_ip") or None


def load_bad_auto_config(config_path=None):
    """bad-auto's standing config (YAML or JSON — stage_red rewrites it as JSON)."""
    path = Path(config_path) if config_path else (BAD_AUTO / "config.yaml")
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data


def apply_local_c2(realm, red_ip):
    """Retarget a realm config block at a red01-hosted tavern (in place).

    c2_ip/base_url/verify_url all become the redteam server itself: the engine
    DNAT points its transport ports at red01, and bad-auto's MCP/verify traffic
    (which runs ON red01) goes over loopback-equivalent addressing.
    """
    realm = realm if isinstance(realm, dict) else {}
    realm["enabled"] = True
    c2_port = int(realm.get("c2_port") or DEFAULT_C2_PORT)
    realm["c2_port"] = c2_port
    realm["c2_ip"] = red_ip
    realm["base_url"] = f"http://{red_ip}:{c2_port}"
    realm["verify_url"] = realm["base_url"]
    return realm


def build_local_c2_config(red_ip, config_path=None):
    """The bad-auto config for a deploy whose tavern lives on red01.

    Returns None when the sibling config is missing or realm is disabled
    outright (provisioning a tavern nothing would use). Also pins
    deploy.red_ip to `red_ip` so the Compfile's `assume_breach_red_ip`
    override is authoritative for BOTH the clone and the DNAT target —
    previously the override only moved the seed's ssh target.
    """
    path = Path(config_path) if config_path else (BAD_AUTO / "config.yaml")
    if not path.exists():
        return None
    cfg = load_bad_auto_config(path)
    realm = cfg.get("realm")
    if realm is not None and realm.get("enabled") is False:
        return None
    apply_local_c2(cfg.setdefault("realm", {}), red_ip)
    cfg.setdefault("deploy", {})["red_ip"] = red_ip
    return cfg


def write_local_c2_config(comp_dir, cfg):
    """Derived config next to the comp's own state; never inside bad-auto's tree.

    Returns an ABSOLUTE path: bad-auto runs with cwd=bad-auto, so a relative
    `--config` resolves inside its own tree, `load_config` finds no file and
    silently falls back to its built-in defaults (red01 then comes up on the
    default red_ip 10.0.0.199 while everything else targets the comp's 10.0.0.198 —
    caught live 2026-10-09, after a 95-minute deploy whose entire red presence died
    with `No route to host`).
    """
    path = (Path(comp_dir) / ".realm-c2-config.yaml").resolve()
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def prepare_local_c2(comp_dir, red_ip):
    """(config_path, realm_block) for a local-C2 deploy, or (None, None).

    Called from plant_assume_breach BEFORE `badauto deploy`, because the engine
    DNAT bakes in `realm.c2_ip` at deploy time.

    The derived config also carries `deploy.engine_ip`: `badauto deploy` needs the
    scoring engine's address to install the DNAT, and its own fallback reads
    `<comp_dir>/credentials.txt` — which only works when that path is absolute
    (see red_plant_ops._deploy_red). Setting it here makes the deployed config
    self-sufficient.
    """
    if not (BAD_AUTO / "config.yaml").exists():
        print("  realm-c2: no bad-auto config.yaml — C2 stays on its configured host")
        return None, None
    cfg = build_local_c2_config(red_ip)
    if cfg is None:
        print("  realm-c2: realm disabled in bad-auto config — no tavern to provision")
        return None, None
    engine_ip = engine_ip_for(comp_dir)
    if engine_ip and not (cfg.get("deploy") or {}).get("engine_ip"):
        cfg.setdefault("deploy", {})["engine_ip"] = engine_ip
    return write_local_c2_config(comp_dir, cfg), cfg["realm"]


# ------------------------------------------------------------- unit generation

def tavern_unit(realm, home, db_password):
    c2_port = int(realm.get("c2_port") or DEFAULT_C2_PORT)
    return f"""[Unit]
Description=Realm C2 teamserver (tavern)
After=network-online.target mariadb.service
Wants=mariadb.service

[Service]
Environment=ENABLE_AI_MCP=1
Environment=HTTP_LISTEN_ADDR=0.0.0.0:{c2_port}
Environment=MYSQL_ADDR=127.0.0.1:3306
Environment=MYSQL_USER=tavern
Environment=MYSQL_PASSWD={db_password}
Environment=MYSQL_DB=tavern
Environment=SECRETS_FILE_PATH={home}/realm/secrets/tavern-secrets
ExecStart={home}/realm/tavern_updated
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def redirector_units(realm):
    """(name, unit-text) for every configured callback transport.

    grpc needs no redirector (the tavern core listener speaks gRPC framing).
    With no `transports` table, ALL callback transports are provisioned — the
    point of this module is a full-transport C2.
    """
    specs = (realm or {}).get("transports")
    if isinstance(specs, dict) and specs:
        enabled = [t for t in TRANSPORT_ORDER
                   if specs.get(t) not in (None, False)]
    else:
        enabled = [t for t in TRANSPORT_ORDER if t != "grpc"]
        specs = {}
    c2_port = int((realm or {}).get("c2_port") or DEFAULT_C2_PORT)
    home = f"/home/{RED_USER}"
    bin_path = f"{home}/realm/tavern_updated"
    units = []
    for name in enabled:
        if name not in REDIRECTOR_UNIT:
            continue  # grpc: core listener only
        spec = specs.get(name) if isinstance(specs.get(name), dict) else {}
        domain = ""
        if name == "icmp":
            port = 0
        else:
            port = int(spec.get("c2_port") or REDIRECTOR_PORTS[name])
            if name == "dns":
                domain = spec.get("domain") or DEFAULT_DNS_DOMAIN
                if not _DOMAIN_RE.match(domain):
                    raise ValueError(f"unsafe dns transport domain: {domain!r}")
        desc = _REDIRECTOR_DESC[name].format(port=port, domain=domain)
        cmd = _REDIRECTOR_CMD[name].format(port=port, domain=domain, c2_port=c2_port)
        units.append((REDIRECTOR_UNIT[name], f"""[Unit]
Description={desc}
After=tavern.service

[Service]
ExecStart={bin_path} {cmd}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""))
    return units


def transport_summary(realm):
    """Human list of the transports this C2 serves (for the deploy summary)."""
    units = redirector_units(realm)
    names = ["grpc"] + [u[0].replace("tavern-", "").replace("-redirector.service", "")
                        for u in units]
    return names


# ------------------------------------------------------------- provision script

def provision_script(realm, *, repo=DEFAULT_REPO, go_version=DEFAULT_GO_VERSION):
    """The bash script run on red01 as the sudo-capable user. Idempotent."""
    c2_port = int((realm or {}).get("c2_port") or DEFAULT_C2_PORT)
    if repo != DEFAULT_REPO and not repo.startswith("https://"):
        raise ValueError(f"realm_c2_repo must be an https URL: {repo!r}")
    if not re.match(r"^\d+\.\d+\.\d+$", go_version):
        raise ValueError(f"realm_c2_go_version must be x.y.z: {go_version!r}")
    units = ([("tavern.service", tavern_unit(realm, "/home/" + RED_USER, DBPW_PLACEHOLDER))]
             + redirector_units(realm))
    unit_blocks = "\n".join(
        f"cat > \"$UDIR/{name}\" <<'UNIT'\n{text}UNIT\n"
        for name, text in units)
    unit_names = " ".join(n for n, _ in units)
    return f"""set -euo pipefail
say() {{ echo "[realm-c2] $*"; }}
REALM_DIR="$HOME/realm"
BIN="$REALM_DIR/tavern_updated"
UDIR="$HOME/.config/systemd/user"

: "${{DBPW:?DBPW env var must carry the tavern DB password}}"

# The tavern build is a from-source Go build. Its caches and scratch default to
# /tmp and ~/go on the ROOT filesystem, and a fresh red01 clone ships a 10G root
# LV — a build dies mid-compile with "mkdir /tmp/go-build…: no space left on
# device" (live 2026-10-08) that reads like a code error. Claim any unallocated
# VG space, keep every Go dir on the disk, and say plainly if headroom is short
# (the operator then resizes red01's disk and grows the filesystem: growpart +
# pvresize + lvextend -r -l +100%FREE + resize2fs).
export TMPDIR="$HOME/.cache/tmp" GOCACHE="$HOME/.cache/go-build" GOMODCACHE="$HOME/go/pkg/mod"
mkdir -p "$TMPDIR" "$GOCACHE" "$GOMODCACHE"
say "growing the root filesystem into unallocated VG space (if any)"
ROOT=$(findmnt -no SOURCE /)
if sudo -n lvs "$ROOT" >/dev/null 2>&1; then
  sudo -n lvextend -r -l +100%FREE "$ROOT" >/dev/null 2>&1 || true
fi
FREE_MB=$(df -Pm / | awk 'NR==2 {{print $4}}')
say "root filesystem: $(df -h / | awk 'NR==2 {{print $2" total, "$4" free"}}')"
if [ "$FREE_MB" -lt 8000 ]; then
  say "WARNING: only ${{FREE_MB}}MB free on / — the tavern build wants ~8GB."
  say "         resize red01's disk, then growpart+pvresize+lvextend -r -l +100%FREE+resize2fs"
fi

if systemctl --user is-active --quiet tavern.service 2>/dev/null && [ -x "$BIN" ]; then
  say "tavern already provisioned and active — nothing to do"
  exit 0
fi

# MariaDB's FIRST init can exceed systemd's 5-min start timeout (VM101 lesson,
# 2026-10-07) — write the drop-in before the package's postinst starts it.
sudo -n mkdir -p /etc/systemd/system/mariadb.service.d
printf '[Service]\\nTimeoutStartSec=1800\\n' | sudo -n tee /etc/systemd/system/mariadb.service.d/timeout.conf >/dev/null
sudo -n systemctl daemon-reload
say "installing apt deps (git curl mariadb-server libcap2-bin)"
sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl mariadb-server libcap2-bin
sudo -n systemctl enable --now mariadb.service
for i in $(seq 1 90); do sudo -n mysqladmin ping >/dev/null 2>&1 && break; sleep 2; done
sudo -n mysqladmin ping >/dev/null

say "creating tavern database + user"
sudo -n mysql -e "CREATE DATABASE IF NOT EXISTS tavern CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'tavern'@'localhost' IDENTIFIED BY '$DBPW';
CREATE USER IF NOT EXISTS 'tavern'@'127.0.0.1' IDENTIFIED BY '$DBPW';
ALTER USER 'tavern'@'localhost' IDENTIFIED BY '$DBPW';
ALTER USER 'tavern'@'127.0.0.1' IDENTIFIED BY '$DBPW';
GRANT ALL PRIVILEGES ON tavern.* TO 'tavern'@'localhost';
GRANT ALL PRIVILEGES ON tavern.* TO 'tavern'@'127.0.0.1';
FLUSH PRIVILEGES;"

if [ ! -x "$HOME/go-toolchain/go/bin/go" ]; then
  say "installing Go {go_version} (apt's is too old for realm)"
  curl -fsSL https://go.dev/dl/go{go_version}.linux-amd64.tar.gz -o /tmp/go.tgz
  mkdir -p "$HOME/go-toolchain"
  tar -C "$HOME/go-toolchain" -xzf /tmp/go.tgz
  rm -f /tmp/go.tgz
fi
export PATH="$HOME/go-toolchain/go/bin:$PATH"
go version

if [ ! -d "$REALM_DIR/.git" ]; then
  if [ -e "$REALM_DIR" ]; then
    say "$REALM_DIR exists but is not a git checkout — refusing to clobber"; exit 1
  fi
  say "cloning {repo}"
  git clone --depth 1 {repo} "$REALM_DIR"
fi

say "building tavern (module download + compile — a few minutes on first run)"
cd "$REALM_DIR"
go build -buildvcs=false -o tavern_updated ./tavern/

say "secrets dir + DB password file"
mkdir -p "$REALM_DIR/secrets"
printf '%s\\n' "$DBPW" > "$REALM_DIR/secrets/tavern-db-pw"
chmod 600 "$REALM_DIR/secrets/tavern-db-pw"

say "icmp transport: setcap + sysctl on the C2 host"
sudo -n setcap cap_net_raw+ep "$BIN"
printf 'net.ipv4.icmp_echo_ignore_all=1\\n' | sudo -n tee /etc/sysctl.d/99-realm-icmp.conf >/dev/null
sudo -n sysctl -w -q net.ipv4.icmp_echo_ignore_all=1

say "writing systemd --user units"
mkdir -p "$UDIR"
{unit_blocks}
say "injecting the tavern DB password into its unit (a quoted heredoc cannot expand it)"
chmod 600 "$UDIR/tavern.service"
sed -i "s|{DBPW_PLACEHOLDER}|$DBPW|" "$UDIR/tavern.service"
if grep -q '{DBPW_PLACEHOLDER}' "$UDIR/tavern.service"; then
  say "FAILED to inject the DB password into tavern.service"; exit 1
fi
sudo -n loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now {unit_names}

sleep 2
fail=0
for u in {unit_names}; do
  if ! systemctl --user is-active --quiet "$u"; then
    say "UNIT FAILED: $u"
    journalctl --user -u "$u" --no-pager | tail -15 || true
    fail=1
  fi
done
[ "$fail" = 0 ] || exit 1
curl -s -o /dev/null http://127.0.0.1:{c2_port}/ || true   # any HTTP answer is fine
ss -tln | grep -q ':8001 ' || {{ say "http1 redirector not listening"; exit 1; }}
ss -uln | grep -q ':5300 ' || {{ say "dns redirector not listening"; exit 1; }}
ss -uln | grep -q ':8443 ' || {{ say "quic redirector not listening"; exit 1; }}
say "OK: tavern + redirectors live (mcp /mcp, core :{c2_port})"
"""


# ------------------------------------------------------------------ execution

def _ssh(ssh_key, target_ip, script, timeout=120, env_prefix=""):
    """Run a command on a range host as the deploy user."""
    return subprocess.run(
        ["ssh", *_ssh_base(ssh_key), f"{RED_USER}@{target_ip}",
         f"{env_prefix}bash -s"], input=script,
        capture_output=True, text=True, timeout=timeout)


def _scp(ssh_key, *args, timeout=180):
    return subprocess.run(
        ["scp", *_ssh_base(ssh_key), *args],
        capture_output=True, text=True, timeout=timeout)


# ------------------------------------------------------- server key propagation

_PUBKEY_RE = re.compile(r"public key: ([A-Za-z0-9+/=]{20,})")
# Merge realm.pubkey into red01's live config (JSON on the VM) through base64, so
# no shell/heredoc quoting can eat it.
_SET_PUBKEY_PY = """
import json
p = '/etc/bad-auto/config.yaml'
with open(p) as fh:
    cfg = json.load(fh)
cfg.setdefault('realm', {})['pubkey'] = '%s'
with open(p, 'w') as fh:
    json.dump(cfg, fh, indent=2)
print('realm.pubkey set on red01')
"""


def tavern_pubkey(red_ip, ssh_key, timeout=90):
    """The tavern server public key (base64) as tavern logs it at startup.

    Every planted beacon must carry it as ``IMIX_SERVER_PUBKEY``: imix encrypts its
    callbacks with the server key and silently falls back to a compiled-in one when
    the env var is unset — a beacon built against a DIFFERENT tavern then registers
    nothing and dies with "failed to decrypt chacha20poly1305". The key is generated
    once (SECRETS_FILE_PATH persists it), so it is stable for the range's life.
    Returns None when it cannot be read.
    """
    run = _ssh(ssh_key, red_ip,
               "journalctl --user -u tavern.service --no-pager 2>/dev/null "
               "| grep -oE 'public key: [A-Za-z0-9+/=]+' | tail -1", timeout=timeout)
    m = _PUBKEY_RE.search((run.stdout or "") + (run.stderr or ""))
    return m.group(1) if m else None


def set_realm_pubkey(ssh_key, red_ip, pubkey, timeout=90):
    """Write ``realm.pubkey`` into red01's /etc/bad-auto/config.yaml.

    The planters run ON red01 and read that file, and the key only exists once
    tavern has generated it — which is AFTER the deploy wrote the config. So the
    first provision of a range pushes it here, before the day-0 seed plants any
    beacon. (`realm.pubkey` is in bad-auto's CONFIG_CONTRACT, so later deploys
    carry it from the operator's config and this becomes a no-op refresh.)
    """
    payload = base64.b64encode((_SET_PUBKEY_PY % pubkey).encode()).decode()
    return _ssh(ssh_key, red_ip,
                f"echo {payload} | base64 -d | sudo -n python3 -", timeout=timeout)


def stage_implants(ssh_key, red_ip, implant_host, realm=None, timeout=900):
    """Relay the prebuilt imix implants implant_host -> red01's staging paths.

    bad-auto's own `_stage_realm` scp's FROM `realm.c2_ip`, which is red01 once
    this module is in play — so the relay happens here instead, into the exact
    paths `realm_plant`/`realm_plant_win` upload from.
    """
    realm = realm or {}
    # bad-auto's realm_plant reads its own install_dir (default /opt/bad-auto/realm)
    # from the config the deploy stages onto red01 — stage into the SAME dir or the
    # plants refuse with "imix not staged".
    install_dir = realm.get("install_dir") or INSTALL_DIR
    win_src = realm.get("win_payload") or WIN_IMPLANT_SRC_DEFAULT
    tmp = tempfile.mkdtemp(prefix="realm-c2-implants-")
    try:
        r1 = _scp(ssh_key, f"{RED_USER}@{implant_host}:{LINUX_IMPLANT_SRC}",
                  f"{tmp}/imix")
        if r1.returncode != 0:
            return {"ok": False, "error":
                    f"could not pull the Linux imix off {implant_host}: "
                    f"{(r1.stderr or r1.stdout or '').strip()[-200:]}"}
        r2 = _scp(ssh_key, f"{RED_USER}@{implant_host}:{win_src}",
                  f"{tmp}/{WIN_IMPLANT_DST}")
        if r2.returncode != 0:
            return {"ok": False, "error":
                    f"could not pull the Windows imix off {implant_host} "
                    f"({win_src}): {(r2.stderr or r2.stdout or '').strip()[-200:]}"}
        _ssh(ssh_key, red_ip, "mkdir -p /tmp/.realm-c2-stage", timeout=30)
        r3 = _scp(ssh_key, f"{tmp}/imix", f"{tmp}/{WIN_IMPLANT_DST}",
                  f"{RED_USER}@{red_ip}:/tmp/.realm-c2-stage/")
        if r3.returncode != 0:
            return {"ok": False, "error":
                    f"could not push implants to red01: "
                    f"{(r3.stderr or r3.stdout or '').strip()[-200:]}"}
        mv = _ssh(ssh_key, red_ip,
                  f"sudo -n mkdir -p {install_dir} && "
                  f"sudo -n mv /tmp/.realm-c2-stage/imix "
                  f"/tmp/.realm-c2-stage/{WIN_IMPLANT_DST} {install_dir}/ && "
                  f"sudo -n chmod 755 {install_dir}/imix "
                  f"{install_dir}/{WIN_IMPLANT_DST} && "
                  f"rm -rf /tmp/.realm-c2-stage", timeout=60)
        if mv.returncode != 0:
            return {"ok": False, "error":
                    f"red01 refused the implant install: "
                    f"{(mv.stderr or mv.stdout or '').strip()[-200:]}"}
        return {"ok": True, "install_dir": install_dir,
                "implants": ["imix", WIN_IMPLANT_DST]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def provision_realm_c2(comp_dir, red_ip, ssh_key, realm, *,
                       repo=DEFAULT_REPO, go_version=DEFAULT_GO_VERSION,
                       implant_host=DEFAULT_IMPLANT_HOST, timeout=2700):
    """Provision tavern + every redirector + MCP on red01. Never raises.

    Returns a summary dict; `ok` False entries carry a one-line `error` for the
    degradation ledger. The DB password is generated per provision and lives
    ONLY on red01 (`~/realm/secrets/tavern-db-pw` + the unit file).
    """
    summary = {"red_ip": red_ip, "c2_port": int(realm.get("c2_port") or DEFAULT_C2_PORT),
               "transports": transport_summary(realm)}
    try:
        implants = stage_implants(ssh_key, red_ip, implant_host, realm)
    except subprocess.TimeoutExpired:
        implants = {"ok": False, "error": "implant staging timed out"}
    summary["implants"] = implants
    if not implants.get("ok"):
        # Non-fatal: realm_plant refuses cleanly until the binaries exist.
        print(f"  realm-c2: WARNING — implant staging failed: {implants.get('error')}")
    db_password = secrets.token_hex(16)
    script = provision_script(realm, repo=repo, go_version=go_version)
    try:
        run = _ssh(ssh_key, red_ip, script, timeout=timeout,
                   env_prefix=f"DBPW={shlex.quote(db_password)} ")
    except subprocess.TimeoutExpired:
        summary.update(ok=False, error=f"provision timed out after {timeout}s")
        return summary
    tail = ((run.stdout or "") + (run.stderr or "")).strip().splitlines()
    summary["log_tail"] = tail[-6:]
    if run.returncode != 0:
        summary.update(ok=False, error=f"provision script rc={run.returncode}: "
                                       f"{' | '.join(tail[-3:])}")
        return summary
    summary["ok"] = True
    summary["mcp"] = "/mcp"
    # Give the planters the key every beacon must encrypt to. Without it imix falls
    # back to a compiled-in key: the beacon plants, looks active, and never
    # registers (tavern answers "failed to decrypt chacha20poly1305").
    pubkey = tavern_pubkey(red_ip, ssh_key)
    if pubkey:
        push = set_realm_pubkey(ssh_key, red_ip, pubkey)
        summary["pubkey"] = pubkey
        if push.returncode != 0:
            print("  realm-c2: WARNING — could not write realm.pubkey on red01 "
                  f"({(push.stderr or push.stdout or '').strip()[-120:]})")
    else:
        print("  realm-c2: WARNING — could not read tavern's public key; planted "
              "beacons will fall back to imix's built-in key and never register")
    return summary
