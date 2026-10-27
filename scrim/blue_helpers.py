
from scrim import core


def mybox_script(comp):
    boxes = core.read_comp_json(comp, "boxes.json")
    proxy = f'"ProxyCommand=ssh -i $KEY_PATH {core.SSH_OPTS} -W %h:%p $VM_USER@$ENGINE_IP"'
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", 'cd "$(dirname "$0")"',
             f'BOX=${{1:?box: {"|".join(b["name"] for b in boxes)}}}; shift',
             "source ./scrim.env", 'case "$BOX" in']
    for b in boxes:
        target = f"192.168.$MY_TID.{b['last_octet']}"
        if core.is_windows_box(b):
            lines.append(
                f'  {b["name"]}) exec sshpass -p "$BOX_PW" ssh {core.SSH_OPTS} -o {proxy} '
                f'"Administrator@{target}" "$@" ;;')
        else:
            lines.append(
                f'  {b["name"]}) exec ssh -i "$KEY_PATH" {core.SSH_OPTS} -o {proxy} '
                f'"$BOX_USER@{target}" "$@" ;;')
    lines.append('  *) echo "unknown box $BOX" >&2; exit 2 ;;')
    lines.append("esac")
    return "\n".join(lines) + "\n"


QLOGIN = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ./scrim.env
tmp=$(mktemp)
curl -s --max-time 15 -c "$tmp" -X POST "http://$ENGINE_IP/api/login" \\
  -H 'Content-Type: application/json' \\
  -d "{\\"username\\":\\"$MY_TEAM\\",\\"password\\":\\"$MY_PW\\"}" >/dev/null
mv "$tmp" "$JAR"
"""


SCORE_PY = '''#!/usr/bin/env python3
"""Print your team's live scoreboard status as plain text. Env from scrim.env (exported)."""
import json
import os
import subprocess
import urllib.request

engine = os.environ["ENGINE_IP"]
team = os.environ["MY_TEAM"]
# scrim.env always exports JAR; the fallback mirrors the driver's run-dir jar location
# (.jars/<account>.jar) instead of the old predictable /tmp/jar.<team> path.
jar = os.environ.get("JAR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".jars", f"{team}.jar")
qlogin = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qlogin")


def call(path, cookie=None):
    req = urllib.request.Request(f"http://{engine}{path}",
                                 headers={"Cookie": cookie} if cookie else {})
    return urllib.request.urlopen(req, timeout=15)


def jar_cookie():
    try:
        for line in reversed(open(jar).read().splitlines()):
            # HttpOnly cookies land in the jar as "#HttpOnly_..." — that's not a
            # comment; only "# "-prefixed lines are. Current Quotient sets HttpOnly.
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 7:
                return f"{parts[-2]}={parts[-1]}"
    except OSError:
        pass
    return None


def load_teams(cookie):
    data = json.loads(call("/api/teams", cookie).read())
    if not isinstance(data, list):
        raise ValueError(f"unexpected payload: {str(data)[:80]}")
    return data


cookie = jar_cookie()
if cookie:
    try:
        teams = load_teams(cookie)
    except Exception:
        cookie = None
if not cookie:
    subprocess.run([qlogin], check=False)
    cookie = jar_cookie()
teams = load_teams(cookie)
tid = next(t["ID"] for t in teams if t["Name"] == team)
for s in json.loads(call(f"/api/services/{tid}", cookie).read()):
    rounds = s.get("Last10Rounds") or []
    checks = (rounds[0] if rounds else {}).get("Checks") or []
    up = bool(checks) and all(c.get("Result") for c in checks)
    err = next((c.get("Error", "") for c in checks if c.get("Error") and not c.get("Result")), "")
    print(f"{'UP  ' if up else 'DOWN'} {s['ServiceName']:16s} {err[:70]}")
'''


MYSCORE = """#!/usr/bin/env bash
cd "$(dirname "$0")"
set -a; source ./scrim.env; set +a
exec python3 "$(dirname "$0")/score.py"
"""


SUBMIT_INJECT = """#!/usr/bin/env bash
cd "$(dirname "$0")"; source ./scrim.env
submit() { curl -s --max-time 30 -b "$JAR" -F "file=@$2" -X POST "http://$ENGINE_IP/api/injects/$1/submit"; }
# Real JSON parse, not a substring probe: an inject whose body legitimately contains the
# text "error" used to trigger a pointless re-login (the driver has the same fix).
is_error() {
  python3 -c 'import json,sys
try: d = json.loads(sys.stdin.read())
except Exception: sys.exit(1)
sys.exit(0 if isinstance(d, dict) and "error" in d else 1)'
}
out=$(submit "$1" "$2")
if printf '%s' "$out" | is_error; then ./qlogin; out=$(submit "$1" "$2"); fi
echo "$out"
"""


OPENCODE_PROJECT_CFG = """{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "{PROVIDER_KEY}": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Scrim LLM",
      "options": {
        "baseURL": "{BASE_URL}",
        "apiKey": "{API_KEY_FIELD}"
      },
      "models": {
        "{MODEL_ID}": {
          "name": "{MODEL_ID}",
          "limit": { "context": {CTX}, "output": {OUT} }{EFFORT}
        }
      }
    }
  }
}
"""


# probes each pinned check from the engine's vantage: blue's own ssh sees "locally healthy" while the scorer sees DOWN
def scorch_script(comp):
    boxes = {b["name"]: b for b in core.read_comp_json(comp, "boxes.json")}
    pins = core.read_comp_json(comp, "box_services.json")
    http_specs, port_specs, ssh_specs = [], [], []
    for box, services in pins.items():
        b = boxes.get(box)
        if not b:
            continue
        ip = f"192.168.$MY_TID.{b['last_octet']}"
        for pin in services:
            if isinstance(pin, dict) and pin.get("plant_only"):
                # plant_only pins are NOT scored: probing them builds an oracle that
                # disagrees with the scoreboard.
                continue
            # Resolve through Quotient's own pin table so the probe matches the emitted
            # check (port, scheme, path, status) instead of guessing from the display name.
            from quotient.setup import _resolve_pin
            _name, check_key, cfg = _resolve_pin(pin)
            if check_key is None:
                continue
            port = cfg.get("Port")
            if not port:
                continue
            if check_key == "Web":
                url = (cfg.get("Url") or [{}])[0]
                http_specs.append(
                    f"{box}|{ip}|{port}|{cfg.get('Scheme') or 'http'}|"
                    f"{url.get('Path') or '/'}|{url.get('Status') or 200}")
            elif check_key in ("Ssh", "Ftp", "Smtp", "Imap", "Sql"):
                ssh_specs.append(f"{box}|{ip}|{port}")
            else:
                port_specs.append(f"{box}|{ip}|{port}")
    lines = ["#!/usr/bin/env bash", "set -uo pipefail", 'cd "$(dirname "$0")"',
             "source ./scrim.env",
             'ENGINE_RUN="ssh -i "$KEY_PATH" -o StrictHostKeyChecking=no '
             '-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 "$VM_USER@$ENGINE_IP""',
             'echo "== scored-check oracle (engine vantage) — team $MY_TEAM =="']
    if not (http_specs or port_specs or ssh_specs):
        # catalog-name pins carry no port metadata — no probe derivable; fall back to the scorer's view so output is never empty
        lines.append('echo "(this comp\'s pins carry no ports, so there is no independent')
        lines.append(' engine-vantage probe — showing the scorer\'s own view instead)"')
        lines.append('exec ./score.py')
        return "\n".join(lines) + "\n"
    if http_specs:
        lines.append("for spec in " + " ".join(f'"{x}"' for x in http_specs) + "; do")
        lines.append('  IFS="|" read -r box ip port scheme path want <<<"$spec"')
        lines.append('  extra=""; [ "$scheme" = https ] && extra="-k"')
        lines.append('  r=$($ENGINE_RUN "curl -sm 5 $extra -o /dev/null -w \'%{http_code}\' '
                     '$scheme://$ip:$port$path" 2>/dev/null)')
        lines.append('  if [ "$r" = "$want" ]; then echo "UP   $box $scheme:$port$path '
                     '(HTTP $r)"; else echo "DOWN $box $scheme:$port$path (HTTP '
                     '${r:-no answer}, want $want)"; fi; done')
    if port_specs:
        lines.append("for spec in " + " ".join(f'"{x}"' for x in port_specs) + "; do")
        lines.append('  IFS="|" read -r box ip port <<<"$spec"')
        lines.append('  if $ENGINE_RUN "nc -w 5 $ip $port </dev/null >/dev/null 2>&1"; '
                     'then echo "UP   $box tcp:$port open"; else echo "DOWN $box tcp:$port closed"; fi; done')
    if ssh_specs:
        lines.append("for spec in " + " ".join(f'"{x}"' for x in ssh_specs) + "; do")
        lines.append('  IFS="|" read -r box ip port <<<"$spec"')
        lines.append('  ok=""')
        lines.append('  for pair in "${CREDLIST[@]}"; do')
        lines.append('    user=${pair%%:*}; pw=${pair#*:}')
        lines.append('    if sshpass -p "$pw" ssh -o StrictHostKeyChecking=no -o LogLevel=ERROR '
                     '-o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 '
                     '-o "ProxyCommand=ssh -i $KEY_PATH -o StrictHostKeyChecking=no '
                     '-o UserKnownHostsFile=/dev/null -W %h:%p $VM_USER@$ENGINE_IP" '
                     '"$user@$ip" true 2>/dev/null; then ok=$user; break; fi; done')
        lines.append('  if [ -n "$ok" ]; then echo "UP   $box ssh:$port (credlist login: $ok)"; '
                     'else echo "DOWN $box ssh:$port — port may be open but the scored credlist '
                     'LOGIN FAILS. If you hardened sshd: allow password auth for the credlist '
                     'account from the gateway only (Match User <acct> Address 192.168.$MY_TID.1 '
                     '/ PasswordAuthentication yes), sshd -t, reload, re-run ./scorch."; fi; done')
    lines.append('echo "(checks replicate the engine vantage; a pin DOWN here means the scorer sees it DOWN)"')
    return "\n".join(lines) + "\n"


NOTEBOOK_TEMPLATE = """# NOTEBOOK — team {n} working memory (update FIRST every cycle)

## SNAPSHOT (one line — current state + what you were about to do next)
(none yet)

## OPEN INCIDENTS
(none yet)

## HUNT CHECKLIST
- [ ] rogue users / UID-0 accounts (/etc/passwd, uid 0 duplicates, fresh /etc/shadow entries)
- [ ] unexpected cron (/etc/cron.d/*, crontab -l, user crontabs)
- [ ] rogue systemd units (/etc/systemd/system, list-unit-files, enabled-but-unfamiliar)
- [ ] sudoers changes (/etc/sudoers, /etc/sudoers.d/*)
- [ ] firewall tampering (iptables/nft rules dropping scored ports, ufw status)
- [ ] unexpected listeners (ss -tlnp vs your known service list)
- [ ] Windows: new local admins, odd services, scheduled tasks (schtasks), HKLM Run keys
- [ ] credential exposure (world-readable files, shell history, stray keys in /root/.ssh)

## DONE
(finished items move here)
"""
