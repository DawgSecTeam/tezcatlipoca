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
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml
from estate import config

REPO = Path(__file__).resolve().parents[1]
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
    from common.utils import compfile_flag, compfile_value  # local: utils imports heavy deps
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
    `TEZ_ENGINE_MGMT_IP` as the fallback.
    """
    try:
        text = (Path(comp_dir) / "credentials.txt").read_text(encoding="utf-8")
    except OSError:
        text = ""
    m = _ENGINE_URL_RE.search(text)
    if m:
        return m.group(1)
    return config.active().engine.mgmt_ip or None


def load_bad_auto_config(config_path):
    """A bad-auto config file from disk (YAML or JSON — the harness writes YAML,
    hand edits have been JSON). Explicit path only: the sibling checkout's
    repo-root config.yaml is standalone-bad-auto state tezcatlipoca no longer
    reads (2026-10-12 cross-session clobber)."""
    path = Path(config_path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data


def default_transport_table(c2_port=DEFAULT_C2_PORT):
    """The full five-transport callback table a local C2 serves.

    gw_port is what the engine DNAT exposes on the team transit
    (172.31.<identifier>.1); c2_port is the red01-side listener (tavern core for
    grpc, a redirector for the rest). Mirrors REDIRECTOR_PORTS and the
    provision script's listen checks.
    """
    return {
        "grpc": {"gw_port": c2_port, "c2_port": c2_port},
        "http1": {"gw_port": REDIRECTOR_PORTS["http1"],
                  "c2_port": REDIRECTOR_PORTS["http1"]},
        "dns": {"gw_port": 53, "c2_port": REDIRECTOR_PORTS["dns"],
                "domain": DEFAULT_DNS_DOMAIN},
        "icmp": {},
        "quic": {"gw_port": REDIRECTOR_PORTS["quic"],
                 "c2_port": REDIRECTOR_PORTS["quic"]},
    }


def team_identifier(comp_dir):
    """The comp's first team identifier from teams.json, or None."""
    try:
        teams = json.loads((Path(comp_dir) / "teams.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for tv in teams.values():
        try:
            return int(tv["identifier"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def callback_ip_for(comp_dir):
    """The engine transit IP the team boxes dial for the C2.

    The in-path firewall hands team <id> the transit 172.31.<id>.1; the engine
    DNATs the transport gw_ports there to red01. Missing teams.json -> None
    (the caller refuses — a beacon DSN without it aims at nothing).
    """
    ident = team_identifier(comp_dir)
    return f"172.31.{ident}.1" if ident is not None else None


def missing_local_c2_keys(realm):
    """The keys whose absence means a DARK C2: transports/callback_ip.

    A realm block without them deploys cleanly, prints "tavern live … all
    transports", seeds `implant: True` flags — and plants one dead legacy http1
    beacon per box with no engine DNAT (live 2026-10-11). Every local-C2 entry
    point validates against this before anything deploys.
    """
    realm = realm or {}
    out = []
    transports = realm.get("transports")
    if not (isinstance(transports, dict) and transports):
        out.append("transports")
    if not realm.get("callback_ip"):
        out.append("callback_ip")
    return out


def synthesize_realm_block(realm, callback_ip=None):
    """Fill in everything a local-C2 realm block needs, in place.

    The operator's sibling config.yaml historically carried a skeletal realm
    block (enabled/base_url at most); deploying that verbatim is the dark-C2
    failure above. Operator-set values win; only gaps get the defaults.
    """
    realm = realm if isinstance(realm, dict) else {}
    c2_port = int(realm.get("c2_port") or DEFAULT_C2_PORT)
    defaults = default_transport_table(c2_port)
    have = realm.get("transports")
    if isinstance(have, dict) and have:
        for t, spec in defaults.items():
            if isinstance(have.get(t), dict):
                for k, v in spec.items():
                    have[t].setdefault(k, v)
            else:
                have[t] = spec
    else:
        realm["transports"] = defaults
    if callback_ip:
        realm.setdefault("callback_ip", callback_ip)
    realm.setdefault("mcp_path", "/mcp")
    realm.setdefault("install_dir", INSTALL_DIR)
    realm.setdefault("interval", 10)
    realm.setdefault("multi_beacon", True)
    realm.setdefault("rawsock_budget", 2)
    return realm


def apply_local_c2(realm, red_ip, callback_ip=None):
    """Retarget a realm config block at a red01-hosted tavern (in place).

    c2_ip/base_url/verify_url all become the redteam server itself: the engine
    DNAT points its transport ports at red01, and bad-auto's MCP/verify traffic
    (which runs ON red01) goes over loopback-equivalent addressing. The FULL
    realm block is synthesized here (transports/callback_ip/multi-beacon knobs)
    so a skeletal operator config can no longer deploy a dark C2.
    """
    realm = realm if isinstance(realm, dict) else {}
    realm["enabled"] = True
    c2_port = int(realm.get("c2_port") or DEFAULT_C2_PORT)
    realm["c2_port"] = c2_port
    realm["c2_ip"] = red_ip
    realm["base_url"] = f"http://{red_ip}:{c2_port}"
    realm["verify_url"] = realm["base_url"]
    return synthesize_realm_block(realm, callback_ip)


def build_local_c2_config(red_ip, config_path=None, comp_dir=None):
    """The bad-auto config for a deploy whose tavern lives on red01.

    Base is the COMPETITION's own config (config_path defaults to it) — the
    sibling checkout's repo-root singleton is no longer a source (2026-10-12
    cross-session clobber). Returns None when that base is missing or realm is
    disabled outright (provisioning a tavern nothing would use). Also pins
    deploy.red_ip to `red_ip` so the Compfile's `assume_breach_red_ip`
    override is authoritative for BOTH the clone and the DNAT target —
    previously the override only moved the seed's ssh target.
    """
    from red import badauto_config_ops
    path = Path(config_path) if config_path else badauto_config_ops.config_path(comp_dir)
    if not path.exists():
        return None
    cfg = load_bad_auto_config(path)
    realm = cfg.get("realm")
    if realm is not None and realm.get("enabled") is False:
        return None
    apply_local_c2(cfg.setdefault("realm", {}),
                   red_ip, callback_ip_for(comp_dir) if comp_dir else None)
    cfg.setdefault("deploy", {})["red_ip"] = red_ip
    return cfg


def write_local_c2_config(comp_dir, cfg):
    """The competition's bad-auto config — one file for the deploy AND the realm
    layer (the old separate .realm-c2-config.yaml is folded into it).

    Returns an ABSOLUTE path: bad-auto runs with cwd=bad-auto, so a relative
    `--config` resolves inside its own tree, `load_config` finds no file and
    silently falls back to its built-in defaults (red01 then comes up on the
    default red_ip while everything else targets the comp's address — caught
    live 2026-10-09, after a 95-minute deploy whose entire red presence died
    with "No route to host").
    """
    from red import badauto_config_ops
    return badauto_config_ops.write_config(comp_dir, cfg)


def prepare_local_c2(comp_dir, red_ip, red_vmid=None):
    """(config_path, realm_block) for a local-C2 deploy, or (None, None).

    Called from plant_assume_breach AFTER it has written the comp's base config
    and BEFORE `badauto deploy`, because the engine DNAT bakes in
    `realm.c2_ip` at deploy time. The layer is applied to the comp's own file,
    so the base identity (competition_dir, red_vmid, engine_ip) survives.

    Raises ValueError when the synthesized block is STILL incomplete (no
    teams.json to derive callback_ip from): deploying it would plant dead
    beacons under a "tavern live" banner, so the caller refuses loudly instead.
    """
    cfg = build_local_c2_config(red_ip, comp_dir=comp_dir)
    if cfg is None:
        print("  realm-c2: no comp config or realm disabled — no tavern to provision")
        return None, None
    if red_vmid is not None:
        cfg.setdefault("deploy", {})["red_vmid"] = red_vmid
    engine_ip = engine_ip_for(comp_dir)
    if engine_ip and not (cfg.get("deploy") or {}).get("engine_ip"):
        cfg.setdefault("deploy", {})["engine_ip"] = engine_ip
    missing = missing_local_c2_keys(cfg["realm"])
    if missing:
        raise ValueError(
            "derived realm block is missing " + "/".join(missing) + " — a local-C2 "
            f"deploy without them plants dead beacons while reporting success "
            f"(teams.json readable? identifier present? comp dir: {comp_dir})")
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

# "active" is NOT enough: tavern writes its secrets file on FIRST start and only
# binds the core port after a restart, so an active-but-unbound tavern must NOT
# short-circuit a repair (live 2026-10-09).
if systemctl --user is-active --quiet tavern.service 2>/dev/null && [ -x "$BIN" ] && ss -tln | grep -q ':{c2_port} '; then
  say "tavern already provisioned, active, and serving :{c2_port} — nothing to do"
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

# Tavern generates secrets/tavern-secrets on its FIRST start and does not bind the
# core port until it is restarted after that write (live 2026-10-09: the unit read
# `active`, :{c2_port} never listened, tavern_pubkey could not read the key, and every
# planted beacon went out keyless — tavern then answered "failed to decrypt
# chacha20poly1305"). Restart once so the core server actually comes up.
say "restarting tavern so it binds the core port after the first-start secrets write"
systemctl --user restart tavern.service

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
core_up=0
for i in $(seq 1 30); do
  if ss -tln | grep -q ':{c2_port} ' && curl -sS -o /dev/null --max-time 5 http://127.0.0.1:{c2_port}/ 2>/dev/null; then
    core_up=1; break
  fi
  sleep 2
done
if [ "$core_up" != 1 ]; then
  say "CORE FAILED: tavern is not serving on :{c2_port} — the planters read its public key from that process"
  journalctl --user -u tavern.service --no-pager | tail -20 || true
  exit 1
fi
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


# The workstation reaches the estate over tailscale at ~30 KB/s bulk, so the
# implant relay streams gzip on the wire and each leg gets a generous bound
# (the old 180 s per-leg scp timeout could not move ~18 MB even uncompressed).
RELAY_LEG_TIMEOUT = 900


def _gzip_pull(ssh_key, host, src, local_gz, timeout=RELAY_LEG_TIMEOUT):
    """`gzip -c` the file on <host> into a local .gz — compressed on the wire."""
    with open(local_gz, "wb") as fh:
        return subprocess.run(
            ["ssh", *_ssh_base(ssh_key), f"{RED_USER}@{host}",
             f"gzip -c -- {shlex.quote(src)}"],
            stdout=fh, stderr=subprocess.PIPE, timeout=timeout)


def _gunzip_push(ssh_key, host, local_gz, dst, timeout=RELAY_LEG_TIMEOUT):
    """Stream a local .gz through `gunzip -c` into `dst` on <host>."""
    with open(local_gz, "rb") as fh:
        return subprocess.run(
            ["ssh", *_ssh_base(ssh_key), f"{RED_USER}@{host}",
             f"mkdir -p /tmp/.realm-c2-stage && gunzip -c > {shlex.quote(dst)}"],
            stdin=fh, stderr=subprocess.PIPE, timeout=timeout)


def _sha256_on(ssh_key, host, path, timeout=90):
    """The file's sha256 as <host> sees it, or None when unreadable."""
    run = _ssh(ssh_key, host, f"sha256sum -- {shlex.quote(path)}", timeout=timeout)
    m = re.search(r"\b[0-9a-f]{64}\b", run.stdout or "")
    return m.group(0) if m and run.returncode == 0 else None


def _relay_err(r, fallback):
    try:
        text = (r.stderr or b"").decode(errors="replace")
    except AttributeError:
        text = r.stderr or ""
    return (text.strip() or fallback)[-200:]


def stage_implants(ssh_key, red_ip, implant_host, realm=None,
                   timeout=RELAY_LEG_TIMEOUT):
    """Relay the prebuilt imix implants implant_host -> red01's staging paths.

    bad-auto's own `_stage_realm` scp's FROM `realm.c2_ip`, which is red01 once
    this module is in play — so the relay happens here instead, into the exact
    paths `realm_plant`/`realm_plant_win` upload from.

    Each leg is gzip-streamed (compressed on the wire), bounded by `timeout`
    seconds per leg, and verified sha256 end-to-end; throughput is printed so a
    slow uplink is visible instead of a mystery 180 s timeout.
    """
    realm = realm or {}
    # bad-auto's realm_plant reads its own install_dir (default /opt/bad-auto/realm)
    # from the config the deploy stages onto red01 — stage into the SAME dir or the
    # plants refuse with "imix not staged".
    install_dir = realm.get("install_dir") or INSTALL_DIR
    win_src = realm.get("win_payload") or WIN_IMPLANT_SRC_DEFAULT
    tmp = tempfile.mkdtemp(prefix="realm-c2-implants-")
    try:
        for label, src, dst in (("linux", LINUX_IMPLANT_SRC, "imix"),
                                ("windows", win_src, WIN_IMPLANT_DST)):
            gz = Path(tmp) / f"{dst}.gz"
            t0 = time.time()
            try:
                r = _gzip_pull(ssh_key, implant_host, src, gz, timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error":
                        f"gzip pull of the {label} implant off {implant_host} timed "
                        f"out after {timeout}s ({src})"}
            if r.returncode != 0:
                return {"ok": False, "error":
                        f"could not pull the {label} implant off {implant_host} "
                        f"({src}): {_relay_err(r, 'no output')}"}
            src_sha = _sha256_on(ssh_key, implant_host, src)
            if not src_sha:
                return {"ok": False, "error": f"could not hash {src} on {implant_host}"}
            pull_sec = time.time() - t0
            t0 = time.time()
            remote_tmp = f"/tmp/.realm-c2-stage/{dst}"
            try:
                r = _gunzip_push(ssh_key, red_ip, gz, remote_tmp, timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error":
                        f"gzip push of the {label} implant to red01 timed out "
                        f"after {timeout}s"}
            if r.returncode != 0:
                return {"ok": False, "error":
                        f"could not push the {label} implant to red01: "
                        f"{_relay_err(r, 'no output')}"}
            push_sec = time.time() - t0
            dst_sha = _sha256_on(ssh_key, red_ip, remote_tmp)
            if dst_sha != src_sha:
                return {"ok": False, "error":
                        f"{label} implant sha256 mismatch across the relay "
                        f"({src_sha} vs {dst_sha}) — re-run staging"}
            nbytes = gz.stat().st_size
            print(f"  realm-c2: {label} implant relayed: {nbytes} B wire bytes, "
                  f"pull {pull_sec:.0f}s ({nbytes / 1024 / max(pull_sec, 0.001):.0f} KiB/s), "
                  f"push {push_sec:.0f}s ({nbytes / 1024 / max(push_sec, 0.001):.0f} KiB/s)")
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


def tavern_pubkey(red_ip, ssh_key, timeout=90, attempts=6, delay=5):
    """The tavern server public key (base64) as tavern logs it at startup.

    Every planted beacon must carry it as ``IMIX_SERVER_PUBKEY``: imix encrypts its
    callbacks with the server key and silently falls back to a compiled-in one when
    the env var is unset — a beacon built against a DIFFERENT tavern then registers
    nothing and dies with "failed to decrypt chacha20poly1305". The key is generated
    once (SECRETS_FILE_PATH persists it), so it is stable for the range's life.

    Retried: on a FIRST provision the units have only just started and tavern prints
    the key a few seconds later, so a single read races it and reports "no key" —
    which then plants beacons that cannot decrypt (caught live 2026-10-09). Returns
    None only when it is still unreadable after the whole window.
    """
    cmd = ("journalctl --user -u tavern.service --no-pager 2>/dev/null "
           "| grep -oE 'public key: [A-Za-z0-9+/=]+' | tail -1")
    for i in range(max(1, attempts)):
        run = _ssh(ssh_key, red_ip, cmd, timeout=timeout)
        m = _PUBKEY_RE.search((run.stdout or "") + (run.stderr or ""))
        if m:
            return m.group(1)
        if i < attempts - 1:
            time.sleep(delay)
    return None


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
