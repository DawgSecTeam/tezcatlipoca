#!/usr/bin/env bash
# tezcatlipoca c3rberus bootstrap — runs ON red01 as root (via sudo bash).
# Idempotent: every step adopts what a previous pass left. Upstream fetches retry
# because the realm NAT flaps (docs/environment-facts.md); a LAN mirror of the two
# big assets on the engine host, if exported as C3R_ASSET_URL, outranks upstream.
# Never edit the c3rberus repo from here: config arrives rendered at /opt/c3rberus-staging.
set -uo pipefail
REPO_URL="__REPO_URL__"
DEST=/opt/c3rberus
CACHE="${C3R_ASSET_URL:-}"                     # e.g. http://10.0.0.143:9998/c3r-assets
STAGE=/opt/c3rberus-staging
LOG=/var/log/tez-c3rberus-bootstrap.log
exec > >(tee -a "$LOG") 2>&1

retry() { # retry <n> <cmd...> — small sleeps first; curl -f's 22 (4xx/404) never retries
  local n=$1 rc i=1; shift
  until "$@"; do
    rc=$?
    [ "$rc" = 22 ] && return 22
    i=$((i+1)); [ $i -gt $n ] && return $rc
    sleep $(( i < 5 ? i * 3 : 30 ))
  done
}

fetch() { # fetch <url> <dest> — LAN cache first, upstream retried after
  [ -s "$2" ] && return 0
  if [ -n "$CACHE" ]; then curl -fsS --max-time 120 "$CACHE/$(basename "$2")" -o "$2" && return 0; fi
  retry 6 curl -fL --retry 4 --retry-all-errors --max-time 900 -o "$2" "$1"
}

echo "== apt base"
export DEBIAN_FRONTEND=noninteractive
for i in 1 2 3; do apt-get update -q && break; sleep 20; done
for i in 1 2 3; do apt-get install -y -q git openssl python3 python3-pip python3-venv \
  python3-yaml python3-bcrypt rust-openssl jq curl ca-certificates nmap ncat netcat-openbsd \
  mingw-w64 clang lld psmisc gcc make g++ pkg-config libssl-dev libffi-dev && break; sleep 20; done

echo "== netexec (pip wheel; the apt line in the repo's setup.sh is Kali-only)"
if ! command -v nxc >/dev/null 2>&1; then
  retry 4 pip3 install --break-system-packages \
    "git+https://github.com/Pennyw0rth/NetExec@1.5.1" || echo "WARNING: nxc install failed"
fi

echo "== sliver server+client"
if [ ! -x /usr/local/bin/sliver-server ]; then
  SLIVER_VER="${SLIVER_VER:-v5.4.40}"
  for b in sliver-server_linux-amd64 sliver-client_linux-amd64; do
    fetch "https://github.com/BishopFox/sliver/releases/download/${SLIVER_VER}/${b}.zip" \
          "/opt/c3rberus-staging/${b}.zip" || exit 1
    mkdir -p /tmp/c3rs && (cd /tmp/c3rs && unzip -o -q "/opt/c3rberus-staging/${b}.zip")
  done
  install -m 0755 /tmp/c3rs/sliver-server_linux-amd64 /usr/local/bin/sliver-server
  install -m 0755 /tmp/c3rs/sliver-client_linux-amd64 /usr/local/bin/sliver-client
fi
# the installer's release also needs its embedded assets unpacked before first Generate
timeout 120 sliver-server unpack --force >/dev/null 2>&1 || true

cat >/etc/systemd/system/sliver.service <<'U'
[Unit]
Description=Sliver C2
After=network-online.target
[Service]
Type=simple
ExecStart=/usr/local/bin/sliver-server daemon
Restart=on-failure
User=root
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload && systemctl enable --now sliver
until timeout 1 bash -c 'cat < /dev/null > /dev/tcp/127.0.0.1/31337'; do sleep 1; done

echo "== c3rberus checkout (LAN forgejo)"
if [ ! -d "$DEST/.git" ]; then
  retry 3 git clone --depth 1 "$REPO_URL" "$DEST" || exit 1
fi
cd "$DEST"
# the committed SQORECARD keypair (and the committed operator cfg + secrets) must go;
# setup.sh only mints certs when none exist, so delete first, then mint.
rm -f certs/cert.pem certs/key.pem sliver_operator.cfg .env
mkdir -p certs db logs implants modules nxc_workspace
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/key.pem -out certs/cert.pem -subj "/CN=red01.tez.internal" >/dev/null 2>&1

echo "== venv + requirements (pypi via the flaky uplink, retried)"
[ -d venv ] || python3 -m venv venv
retry 5 venv/bin/pip install -q -r requirements.txt \
  || echo "WARNING: some python deps did not land — backend health will tell"

echo "== sliver operator config"
if [ ! -f sliver_operator.cfg ]; then
  sliver-server operator --name c3rberus --lhost localhost --permissions all \
    --save "$DEST/sliver_operator.cfg" >/dev/null 2>&1
  chmod 600 sliver_operator.cfg
fi

echo "== config from staging (rendered by the engine, 0600)"
install -m 0600 "$STAGE/inventory.yml" "$DEST/config/inventory.yml"
install -m 0600 "$STAGE/env" "$DEST/.env"

cat >/etc/systemd/system/c3rberus-backend.service <<'U'
[Unit]
Description=C3rberus scoring backend
After=sliver.service network-online.target
[Service]
Type=simple
WorkingDirectory=/opt/c3rberus
EnvironmentFile=-/etc/environment
ExecStartPre=/bin/bash -c 'ulimit -n 8192'
ExecStart=/opt/c3rberus/venv/bin/uvicorn fastapi_backend.main:app --host 0.0.0.0 \
  --port 3000 --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem \
  --backlog 4096 --limit-concurrency 500 --timeout-keep-alive 15
Restart=on-failure
User=root
[Install]
WantedBy=multi-user.target
U
systemctl daemon-reload && systemctl enable --now c3rberus-backend
echo "== bootstrap done"
