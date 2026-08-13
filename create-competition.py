# Author: Hamza

import json
import math
import os
import random
import re
import shlex
import string
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote as url_quote

import requests
import toml
import urllib3
from dotenv import load_dotenv

from quotient.setup import build_event_conf, create_injects, seed_teams, unpause_engine
from utils import DNS_FIX_CMD, load_compfile, pick_competition

ENV_PATH = Path(".env")
NAKON_DIR = Path("nakon")

# Terraform reads these straight out of the process environment (TF_VAR_<name>) — loading
# .env here also makes them available to the `terraform` subprocess calls below, since
# subprocess inherits the parent's environment by default.
load_dotenv(ENV_PATH)

# Proxmox's API token auth (below) talks straight to the API over the same self-signed cert
# main.tf's provider block sets insecure=true for — same tradeoff, just from Python instead.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

### Helper functions

def load_previous_competitions():
   return [
      p.name
      for p in Path("competitions").iterdir()
      if p.is_dir() and (p / "Compfile").exists()
   ]


def random_password():
    return "".join(random.choices(string.ascii_letters + string.digits, k=12))


def collect_teams(number_of_teams):
    teams = {}
    for i in range(1, number_of_teams + 1):
        key = f"team{i}"
        identifier = str(100 + i)
        password = random_password()
        teams[key] = {"identifier": identifier, "password": password}
    return teams


def update_env(updates: dict):
    # Rewrites TF_VAR_* lines in .env in place (preserving comments/order) and mirrors the
    # change into os.environ so the `terraform` subprocess calls below see it immediately —
    # they were already loaded once at import time, before these values were known.
    text = ENV_PATH.read_text()
    for key, value in updates.items():
        line = f"{key}={value}"
        # Pass `line` as a function replacement, not a string: re.sub interprets
        # backslashes/group refs (\1, \g<0>) in a string replacement, which would crash
        # or corrupt .env for values like a free-form event_name containing a backslash.
        new_text, count = re.subn(rf"^{re.escape(key)}=.*$", lambda _m: line, text, flags=re.MULTILINE)
        text = new_text if count else text + f"\n{line}\n"
        os.environ[key] = value
    ENV_PATH.write_text(text)


def list_proxmox_templates():
    # Mirrors main.tf's own lookup (data.proxmox_virtual_environment_vms.templates filters on
    # tag "template" alone) so the picker only ever offers names Terraform will actually
    # resolve. The scoring engine's own template is excluded — it's looked up by
    # TF_VAR_template_vm_id instead, even though it may carry the same tag.
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    scoring_template_id = int(os.environ["TF_VAR_template_vm_id"])
    try:
        r = requests.get(
            f"{endpoint}/api2/json/cluster/resources",
            params={"type": "vm"},
            headers={"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"},
            verify=False, timeout=10,
        )
        r.raise_for_status()
        vms = r.json()["data"]
    except Exception as e:
        print(f"  (couldn't list Proxmox templates: {e})")
        return []
    return sorted(
        vm["name"] for vm in vms
        if vm.get("template") == 1
        and "template" in (vm.get("tags") or "").split(";")
        and vm.get("vmid") != scoring_template_id
    )


def proxmox_api(method, path, **kwargs):
    endpoint = os.environ["TF_VAR_proxmox_endpoint"].rstrip("/")
    url = f"{endpoint}/api2/json{path}"
    headers = {"Authorization": f"PVEAPIToken={os.environ['TF_VAR_proxmox_api_token']}"}
    # This host has shown occasional transient network blips under load (pveproxy dropping a
    # connection mid-request, a `RemoteDisconnected` with no HTTP response at all) — seen live
    # killing an otherwise-healthy long-running deploy at a routine task-status poll. That's not
    # an API error (no status code to even check) so it can't be told apart from a real outage
    # by response content; a few short retries absorb the blip without masking a genuinely dead
    # host; a call that's ACTUALLY down still exhausts these fast and raises as before.
    last_exc = None
    for attempt in range(4):
        try:
            r = requests.request(method, url, headers=headers, verify=False, timeout=60, **kwargs)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def wait_for_proxmox_task(node, upid, timeout=1800):
    # 600s used to be the default; seen live on this host twice now — a VM clone that ran past
    # 90 minutes under concurrent disk contention (host-level, not this tool's doing — see the
    # 2026-08 incident notes) and, separately, an ordinary VM delete that took a bit over 600s
    # with nothing wrong (the task itself reported exitstatus OK once checked directly). A
    # genuinely wedged task still needs a human/manual unlock regardless of the number here; this
    # bump just stops an everyday slow patch on this host from failing a run outright.
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Proxmox task {upid} timed out after {timeout}s")
        data = proxmox_api("GET", f"/nodes/{node}/tasks/{upid}/status")["data"]
        if data["status"] == "stopped":
            if data.get("exitstatus") != "OK":
                raise RuntimeError(f"Proxmox task {upid} failed: {data.get('exitstatus')}")
            return
        time.sleep(3)


def diagnose_unreachable_box(node, vmid):
    """Best-effort diagnosis for a box that never became SSH-reachable, using the QEMU guest
    agent — it works over virtio-serial regardless of network state, so it can tell us WHY a box
    has no route (broken cloud-init, no IP at all, ...) instead of just that it's unreachable.
    A blocked/failed cloud-init that never brings up the NIC looks identical to a slow-booting
    box from the outside (both are "no route to host" for the full retry budget); this is the
    difference between finding that out in seconds versus after 8-attempt retry loops in several
    downstream steps all fail the same way. Never raises — purely diagnostic, and the guest
    agent itself may be unreachable (e.g. not yet started, or genuinely no network at all).
    """
    try:
        ifaces = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )["data"]["result"]
        addrs = [
            a["ip-address"] for iface in ifaces for a in iface.get("ip-addresses", [])
            if a.get("ip-address-type") == "ipv4" and not a["ip-address"].startswith("127.")
        ]
        iface_summary = f"has IPv4 {', '.join(addrs)}" if addrs else "has NO IPv4 address on any interface"
    except Exception as e:
        return f"      (guest agent unreachable for vmid {vmid}, can't diagnose further: {e})"

    cloud_init_summary = "(cloud-init status unavailable)"
    try:
        pid = proxmox_api(
            "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
            data={"command": ["cloud-init", "status", "--long"]},
        )["data"]["pid"]
        deadline = time.time() + 10
        while time.time() < deadline:
            s = proxmox_api(
                "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
                params={"pid": pid},
            )["data"]
            if s.get("exited"):
                out = (s.get("out-data") or "").strip()
                cloud_init_summary = out.splitlines()[0] if out else "(no output)"
                break
            time.sleep(0.5)
    except Exception:
        pass  # keep the default "(cloud-init status unavailable)" — non-fatal either way

    return f"      guest agent (vmid {vmid}): {iface_summary}; cloud-init {cloud_init_summary}"


def guest_agent_exec_root(node, vmid, script, timeout=60):
    """Run a bash script as root via the QEMU guest agent (virtio-serial, not the network).

    The agent daemon is root's own process, so this needs no sudo at all — unlike ssh_via_gateway
    + a script full of `sudo` commands, it isn't affected by a box's *own* sudo trust being
    broken (e.g. the writable-sudoers misconfig makes /etc/sudoers.d insecure, so modern sudo
    refuses to honor sudo-nopasswd's NOPASSWD rule and demands a real password that doesn't
    exist for the key-only cloud-init user — see fix_services_on_boxes). Deliberately does NOT
    "fix" that misconfig; it just gives our own provisioning a channel that doesn't depend on it,
    so the vuln stays intact for whoever's meant to find it.

    Returns (exit_code, stdout, stderr); raises on a guest-agent-level failure (agent
    unreachable, exec never returned) since callers should treat that differently from the
    *command* failing.
    """
    pid = proxmox_api(
        "POST", f"/nodes/{node}/qemu/{vmid}/agent/exec",
        data={"command": ["bash", "-c", script]},
    )["data"]["pid"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status",
            params={"pid": pid},
        )["data"]
        if s.get("exited"):
            return s.get("exitcode", -1), s.get("out-data", ""), s.get("err-data", "")
        time.sleep(1)
    raise RuntimeError(f"guest-agent exec on vmid {vmid} didn't finish within {timeout}s")


# VM IDs are 200 + identifier*10 + box_index (mirrored in main.tf's team_box.vm_id), which
# leaves each team a stride of exactly 10. An 11th box would land on the next team's first box
# and silently clobber it, so the scheme caps the box count rather than the picker.
MAX_BOXES_PER_TEAM = 10

# Team identifiers are `100 + i` and become the subnet's third octet (192.168.<identifier>.x),
# which must stay a valid, non-zero octet (1-254) — i.e. i <= 154. Past that, identifiers like
# 256 would produce an invalid IP and silently break the whole range.
MAX_TEAMS = 154


def vm_id_for(identifier, box_index):
    return 200 + int(identifier) * 10 + box_index


def stop_vm(node, vmid):
    # Graceful first so the box's filesystem is consistent for the clone, but never
    # indefinitely: a shutdown is an ACPI power-button event, and a guest without acpid (box
    # templates are only required to have working cloud-init) just ignores it. That used to
    # stall here for the full task timeout and then abort the deploy — after nakon had already
    # run — so fall back to pulling the plug instead.
    try:
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/shutdown")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)
    except RuntimeError:
        print(f"    vmid {vmid} ignored ACPI shutdown — forcing stop")
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid, timeout=120)


def destroy_vm_if_exists(node, vmid):
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    vm = next((v for v in vms if v["vmid"] == vmid), None)
    if vm is None:
        return
    if vm.get("status") == "running":
        upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
        wait_for_proxmox_task(node, upid)
    upid = proxmox_api("DELETE", f"/nodes/{node}/qemu/{vmid}")["data"]
    wait_for_proxmox_task(node, upid)


def destroy_bridge_if_exists(node, bridge_name):
    """Delete a Proxmox Linux bridge if it exists."""
    try:
        proxmox_api("DELETE", f"/nodes/{node}/network/{bridge_name}")
    except requests.exceptions.HTTPError as e:
        # Swallowing every exception here used to hide real failures (bad/expired token,
        # permission denied) behind "bridge doesn't exist" — a bridge that actually failed to
        # delete was then treated as already gone. A 404 genuinely means "no such bridge" and
        # is the only case worth staying silent about; anything else at least gets a warning.
        if e.response is None or e.response.status_code != 404:
            print(f"    WARNING: could not delete bridge {bridge_name}: {e}")
    except Exception as e:
        print(f"    WARNING: could not delete bridge {bridge_name}: {e}")


def _prompt_int(prompt, default):
    """int(input()) with a default on blank and a re-prompt (not a crash) on non-numeric input."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"  Enter a whole number (or leave blank for {default}).")


def _prompt_optional_int(prompt):
    """Like _prompt_int, but blank means None (no default) instead of a fallback value."""
    while True:
        raw = input(prompt).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            print("  Enter a whole number, or leave blank.")


def collect_boxes():
    # boxes_per_team is per-competition, not global — different events get different boxes.
    # Asked fresh every time a competition is created; reusing one replays its saved boxes.json.
    templates = list_proxmox_templates()
    if templates:
        print("Available box templates (tagged 'template' in Proxmox):")
        for i, t in enumerate(templates, 1):
            print(f"  [{i}] {t}")
        print()
    else:
        print("  (no templates found in Proxmox — you'll need to type template names manually)\n")

    while True:
        raw = input("How many box types for this competition? ").strip()
        try:
            number_of_boxes = int(raw)
        except ValueError:
            print("  Enter a whole number.")
            continue
        if 1 <= number_of_boxes <= MAX_BOXES_PER_TEAM:
            break
        print(f"  Enter a number from 1 to {MAX_BOXES_PER_TEAM} (see MAX_BOXES_PER_TEAM).")

    boxes = []
    for i in range(1, number_of_boxes + 1):
        print(f"\n─── Box {i} of {number_of_boxes} " + "─" * 40)

        existing_names = {b["name"] for b in boxes}
        name = input("  Name (e.g. web01, db, mail): ").strip()
        while not name or name in existing_names:
            if name in existing_names:
                name = input(f"  '{name}' is already used by another box in this competition"
                              " — pick a different name: ").strip()
            else:
                name = input("  Name can't be blank: ").strip()

        if templates:
            while True:
                raw = input(f"  Template [{1}–{len(templates)}, or name]: ").strip()
                try:
                    idx = int(raw)
                    if 1 <= idx <= len(templates):
                        template = templates[idx - 1]
                        break
                except ValueError:
                    pass
                # Also accept a template name string that matches list_proxmox_templates()
                # exactly — removes the fragile index-only selection. Index still works.
                if raw in templates:
                    template = raw
                    break
                print(f"  Enter a number from 1 to {len(templates)}, or a template name.")
        else:
            template = input("  Template name: ").strip()
            while not template:
                template = input("  Template can't be blank — must match a tagged Proxmox VM exactly: ").strip()

        cpu = _prompt_int("  CPU cores    [1]: ", 1)
        memory_mb = _prompt_int("  Memory (MB)  [2048]: ", 2048)
        # Blank keeps the template's own disk. Terraform only emits a disk block when this is
        # set (main.tf), because Proxmox cannot shrink a disk — a value below the template's
        # own size fails the clone.
        disk_gb = _prompt_optional_int("  Disk (GB)    [keep template's]: ")

        box = {
            "name": name, "last_octet": i + 1, "cpu": cpu, "memory_mb": memory_mb,
            "disk_gb": disk_gb, "template": template,
        }
        boxes.append(box)
    return boxes


def load_injects(comp_dir):
    """Load per-competition injects from competitions/<id>/injects/.

    Each inject is a subdirectory containing `inject.json`:
        {
          "title": "...", "description": "...",   # description may instead live in a
          "description_file": "prompt.md",         # sibling file (markdown), optional
          "open_offset_min": 0, "due_offset_min": 60, "close_offset_min": 90
        }
    Any other files in the subdirectory (e.g. a template .docx, a PoC) are uploaded as the
    inject's attachments. Offsets are minutes relative to competition start (now); they're
    resolved to RFC3339 timestamps here so Quotient's CreateInject can parse them directly.
    Returns [] when there's no injects/ dir, so competitions without injects are unaffected.
    """
    injects_dir = comp_dir / "injects"
    if not injects_dir.is_dir():
        return []

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    injects = []
    for sub in sorted(injects_dir.iterdir()):
        manifest = sub / "inject.json"
        if not sub.is_dir() or not manifest.exists():
            continue
        meta = json.loads(manifest.read_text())

        description = meta.get("description", "")
        if meta.get("description_file"):
            desc_path = sub / meta["description_file"]
            if desc_path.exists():
                description = desc_path.read_text()

        def rfc3339(minutes):
            return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Attachments: every file in the folder except the manifest / description source.
        skip = {"inject.json", meta.get("description_file")}
        files = [str(f) for f in sorted(sub.iterdir()) if f.is_file() and f.name not in skip]

        injects.append({
            "title":       meta["title"],
            "description": description,
            "open_time":   rfc3339(meta.get("open_offset_min", 0)),
            "due_time":    rfc3339(meta.get("due_offset_min", 60)),
            "close_time":  rfc3339(meta.get("close_offset_min", 90)),
            "files":       files,
        })
    return injects


def load_boxes(comp_dir):
    # Saved by this script at competition-creation time (see boxes.json below) — reusing a
    # competition replays the exact boxes it was built with, not whatever's currently in .env.
    path = comp_dir / "boxes.json"
    return json.loads(path.read_text()) if path.exists() else None


def confirm_deploy(name, scenario, difficulty, teams, boxes):
    n = len(teams)
    team_range = f"team1–team{n}" if n > 1 else "team1"
    short_scenario = scenario[:72] + ("..." if len(scenario) > 72 else "")

    print("\n─── Ready to deploy " + "─" * 44)
    print(f"  Competition : {name}")
    print(f"  Scenario    : {short_scenario}")
    print(f"  Difficulty  : {difficulty} / 10")
    print(f"  Teams       : {n}  ({team_range}, passwords auto-generated)")
    print(f"  Boxes       :")
    for b in boxes:
        print(f"    {b['name']} — {b['template']}  ({b['cpu']} CPU, {b['memory_mb']} MB)")
    print()
    print("  Terraform will now run; this takes several minutes.")
    answer = input("  Continue? (y/n): ").strip().lower()
    return answer == "y"


def generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password):
    # Import nakon as a package. This used to load nakon/randomize_config.py by file path with
    # importlib and then reach through it for `nr.mysql.connector` — which meant the shim had to
    # keep an otherwise-unused `import mysql.connector` alive purely so this line worked, and any
    # move of the module broke competition generation before it started.
    import mysql.connector

    sys.path.insert(0, str(NAKON_DIR.resolve()))
    from nakon.catalog.randomize import (
        load_configurations,
        os_to_platform,
        pick_configurations,
    )

    load_dotenv(NAKON_DIR / ".env")
    mydb = mysql.connector.connect(
        host=os.getenv("host"), user=os.getenv("user"),
        password=os.getenv("password"), database=os.getenv("database"),
    )
    cursor = mydb.cursor()
    name_to_row = load_configurations(cursor)
    cursor.close()
    mydb.close()

    # Services that are legitimate in the catalog but too heavy/slow for an automated apply
    # (splunk pulls a ~500 MB installer per box; roundcube drags in apache+mariadb+php). They
    # can still be assigned by hand via box_services.json — they're only excluded from the
    # random auto-pick so a hands-off deploy stays fast and reliable.
    SLOW_SERVICES = {"splunk", "roundcube"}
    for svc in SLOW_SERVICES:
        name_to_row.pop(svc, None)

    services_path = comp_dir / "box_services.json"
    vulns_path = comp_dir / "box_vulns.json"

    # Deterministic re-runs: if this competition already pins its configurations, honour them
    # instead of re-randomising. Lets an operator pin an exact set (and makes reusing a
    # competition reproduce the same boxes, which is the documented intent for boxes-per-event).
    #
    # Either file on its own is enough to count as pinned. This used to key off box_services.json
    # alone, which meant an agent (or a person) who wrote only box_vulns.json got silently
    # ignored: the run took the randomise branch and then overwrote their choices.
    if services_path.exists() or vulns_path.exists():
        pinned = json.loads(services_path.read_text()) if services_path.exists() else {}
        # box_services.json holds the scoreable services (that's all Quotient needs); the
        # misconfigs/vulns nakon plants live in box_vulns.json, so a pinned competition can
        # deploy misconfigurations rather than services alone.
        pinned_vulns = json.loads(vulns_path.read_text()) if vulns_path.exists() else {}
        box_configs = {
            box["name"]: (pinned.get(box["name"], []), pinned_vulns.get(box["name"], []))
            for box in boxes
        }
        pinned_from = ", ".join(
            p.name for p in (services_path, vulns_path) if p.exists()
        )
        print(f"  Using pinned configurations from {pinned_from}")
        # fix_services_on_boxes()/push_event_conf() both do an unconditional read of
        # box_services.json later in the deploy. If only box_vulns.json was pinned, that file
        # was never written and those reads crashed with FileNotFoundError. Always write both
        # (even `{}` for a box with no pinned services) so the pinned branch is self-contained,
        # same as the randomize branch below.
        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        vulns_path.write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )
    else:
        # Randomize once per box type so every team defends the same service set,
        # which lets Quotient use its 192.168._.N wildcard IP pattern uniformly.
        box_configs = {}
        for box in boxes:
            platform = os_to_platform(box["template"])
            services, vulns, _ = pick_configurations(
                name_to_row, platform, max(math.ceil(difficulty / 3), 1), max(difficulty, 1)
            )
            box_configs[box["name"]] = (services, vulns)

        # Persist the scoreable services so push_event_conf() can build Quotient checks
        # without re-querying the DB on subsequent runs.
        services_path.write_text(
            json.dumps({name: svcs for name, (svcs, _) in box_configs.items()}, indent=2)
        )
        # Also persist the planted misconfigs/vulns to the companion box_vulns.json. Without this
        # the vulns were re-picked (or, on a reused competition, dropped entirely — the pinned
        # branch reads box_vulns.json), so a reused competition planted services only and lost
        # its misconfigs. Pinning both makes re-runs fully deterministic.
        (comp_dir / "box_vulns.json").write_text(
            json.dumps({name: vulns for name, (_, vulns) in box_configs.items()}, indent=2)
        )

    # Some vulns intentionally break outbound name resolution or the package manager itself
    # (resolv-conf-null-dns; apt-sources-empty, apt-hold-all-packages, dpkg-broken-hold-state).
    # Nakon runs a box's configurations in list order, and pick_configurations()'s vulns list has
    # no notion of "this needs the network/apt" vs "this breaks the network/apt" — so a box that
    # draws both a disruptive vuln and a package-installing one (e.g. redis-no-auth's `apt-get
    # install redis-server`) can have the installer land after DNS/apt is already dead, failing
    # with "Temporary failure resolving ..." or a held/broken package error. None of these configs
    # is buggy on its own; it's purely an ordering artifact of concatenation order. Schedule known
    # disruptive configs last so anything needing package installs still has a working network
    # and package manager when it runs. list.sort with this key is stable, so relative order is
    # otherwise unchanged.
    DISRUPTIVE_CONFIGS = {
        "resolv-conf-null-dns", "apt-sources-empty", "apt-hold-all-packages",
        "dpkg-broken-hold-state",
    }

    machines = []
    for i, (team, box) in enumerate(
        ((t, b) for t in teams.values() for b in boxes), start=1
    ):
        services, vulns = box_configs[box["name"]]
        configurations = services + vulns
        configurations.sort(key=lambda c: c in DISRUPTIVE_CONFIGS)
        machines.append({
            "id": i,
            "name": f"{box['name']}-team{team['identifier']}",
            "ip": f"192.168.{team['identifier']}.{box['last_octet']}",
            "os": box["template"],
            "user": "ubuntu",
            "password": box_password,
            "configurations": configurations,
        })

    # The machine list belongs to this competition, not to nakon's checkout. Writing it into
    # nakon/config.json made that directory shared mutable state: only one competition could be
    # "current", nothing recorded which one it was, and a crash mid-run left the next run reading
    # somebody else's machines.
    config_path = comp_dir / "nakon-config.json"
    config_path.write_text(json.dumps({"machines": machines}, indent=2))
    return config_path


def build_nakon_bundle(config_path):
    """Build (or reuse) the Nakon bundle for this competition. Returns its directory.

    Runs on the operator machine because it is the half that needs the vulndb. The bundle is
    content-addressed: if nothing in the catalog has changed since the last build, this is a
    cache hit and costs one round of MySQL queries.

    That property is what makes --from-phase resume safe. box_services.json / box_vulns.json
    pin the selection, so a resumed run generates the same machine list, which produces the
    same request keys, which hits the same bundle. Phase 5 (team1 only) and phase 6 (every
    team) therefore deploy provably identical content.

    Runs with cwd=NAKON_DIR for one reason only: `nakon build` loads nakon/.env for the vulndb
    credentials. The machine list is passed as an absolute path from the competition directory,
    and --out stays inside nakon because bundles are content-addressed and immutable — sharing
    that directory across competitions is what makes a rebuild a cache hit.
    """
    result = subprocess.run(
        [sys.executable, "-m", "nakon", "build",
         "--config", str(Path(config_path).resolve()),
         "--out", "bundles",
         "--json"],
        cwd=str(NAKON_DIR), capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError(
            "nakon build failed — the vulndb (MySQL + vulndb-ui) has to be reachable from "
            "this machine. Check nakon/.env."
        )

    # --json prints its summary as the final line, after any log output.
    info = json.loads(result.stdout.strip().splitlines()[-1])
    state = "cached" if info["cached"] else "fresh"
    print(f"  Nakon bundle {info['bundle_id'][:12]} ({state}, {info['plans']} plan(s), "
          f"{info['machines']} machine(s))")
    # info["path"] is "bundles/<bundle_id>", relative to the build's cwd (NAKON_DIR) — the
    # bundle id alone (Path(...).name) isn't enough, the "bundles/" directory is part of it.
    return NAKON_DIR / info["path"]


# nakon deploys the machines in a run sequentially, one full plan after another (not in
# parallel) — this is the per-machine time budget deploy()'s phase-5/6 run_nakon() calls scale
# against, so a bigger box_vulns.json (more configs, more package installs per box) grows the
# timeout instead of racing a flat constant that was only ever sized for the original ~4
# configs/box competitions.
PER_MACHINE_NAKON_BUDGET = 2400


def run_nakon(key, scoring_user, scoring_ip, bundle, config_path, only=None, timeout=2400):
    """Push the prebuilt bundle to the scoring engine and deploy from it.

    The engine is the only host that routes into the isolated team subnets, so Nakon has to
    run there — but it no longer needs anything from the vulndb. What used to be copied over
    (deploy.py, configurations.py and, critically, nakon/.env with the database password) is
    replaced by a self-contained bundle, and the engine's pip install drops to paramiko.

    `only` restricts the deploy to those machine names. Phase 5 uses it because team2+ don't
    exist yet; what gets applied to each machine is fixed by the bundle either way.
    """
    ssh_base = [
        "ssh", "-i", str(key),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        f"{scoring_user}@{scoring_ip}",
    ]

    # /tmp/nakon was never created before being scp'd into — multi-source scp into a
    # non-existent directory fails, so this only ever worked on an engine that happened to
    # have the directory left over from an earlier run.
    subprocess.run(ssh_base + ["rm -rf /tmp/nakon && mkdir -p /tmp/nakon"],
                   check=True, timeout=60)

    subprocess.run(
        [
            "scp", "-i", str(key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-r",
            str(NAKON_DIR / "nakon"),           # the package itself
            str(bundle),                        # bundles/<bundle_id>/
            str(Path(config_path).resolve()),   # addresses + credentials only
            f"{scoring_user}@{scoring_ip}:/tmp/nakon/",
        ],
        check=True, timeout=600,
    )

    remote_config = f"/opt/nakon/{Path(config_path).name}"
    only_args = ""
    if only:
        only_args = " --only " + " ".join(shlex.quote(name) for name in only)

    try:
        subprocess.run(
            ssh_base + [
                "sudo mkdir -p /opt/nakon && sudo rm -rf /opt/nakon/* && "
                "sudo cp -r /tmp/nakon/. /opt/nakon/ && "
                "sudo pip3 install --break-system-packages paramiko 2>/dev/null; "
                "cd /opt/nakon && sudo python3 -m nakon deploy "
                f"--bundle /opt/nakon/{bundle.name} --config {remote_config}{only_args}"
            ],
            check=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Killing OUR ssh client on a client-side timeout does not reliably kill the remote
        # `sudo python3 -m nakon deploy` — it's the foreground process of a `sudo`'d session on
        # the engine, not something we hold a direct handle to, and a dropped SSH connection can
        # take a while (or, with a lingering sudo/setsid quirk, never) to deliver SIGHUP to it.
        # Left running, it keeps mutating team boxes in the background while a resume/retry
        # starts a SECOND deploy against the same machines — two nakon processes racing apt/dpkg
        # on the same box, which is consistent with corrupted state seen in practice (packages
        # apt-marked held from a run that otherwise never got far enough to install them).
        # Best-effort pkill before propagating the timeout, so a caller that resumes doesn't
        # inherit an orphaned process it doesn't know exists.
        try:
            subprocess.run(ssh_base + ["sudo pkill -9 -f 'nakon deploy' || true"],
                           timeout=30)
        except Exception:
            pass
        raise


def read_terraform_ctx():
    raw = subprocess.run(
        ["terraform", "output", "-json"], cwd="terraform", capture_output=True, text=True, check=True
    ).stdout
    ctx = json.loads(json.loads(raw)["agent_context"]["value"])
    # ssh_key_path is relative to terraform/ (where Terraform's file() resolves it) —
    # resolve it to an absolute path so subprocess calls from the project root work.
    key_path = ctx["ssh_key_path"]
    if not os.path.isabs(key_path):
        ctx = {**ctx, "ssh_key_path": str((Path("terraform") / key_path).resolve())}
    return ctx


def ssh_via_gateway(ctx, target_ip, cmd, timeout=60):
    """SSH to a target box via the scoring engine gateway.

    The team boxes have 'ubuntu' user with the proxmox key authorized (set by cloud-init).
    We use ProxyCommand through the scoring engine with OpenSSH's built-in -W (direct-stream-local).
    Requires AllowTcpForwarding=yes on the gateway sshd (set in bootstrap_scoring_engine).
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-o", f"ProxyCommand={proxy}",
            f"ubuntu@{target_ip}", cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def ssh_on_gateway(ctx, cmd, timeout=30):
    """Run a command directly on the scoring engine gateway."""
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    return subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            f"{scoring_user}@{scoring_ip}", cmd,
        ],
        capture_output=True, text=True, timeout=timeout,
    )


def wait_for_ssh(key, user, host, timeout=300):
    """Poll a host's SSH until it accepts a command, replacing a blind post-boot sleep.

    Returns True once `ssh user@host true` succeeds, False on timeout. Never raises — a
    timeout prints a warning and the caller proceeds (matching the surrounding "continue
    anyway" error posture), because the later steps have their own retry loops.
    """
    print(f"  Waiting for {host} to accept SSH (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            r = subprocess.run(
                ["ssh", "-i", key,
                 "-o", "StrictHostKeyChecking=no",
                 "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=10",
                 "-o", "BatchMode=yes",
                 f"{user}@{host}", "true"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                print(f"    {host} reachable via SSH (after {attempt} attempt(s))")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    print(f"  WARNING: {host} not reachable via SSH within {timeout}s — continuing anyway")
    return False


def wait_for_boxes_ssh(ctx, teams, boxes, timeout=300):
    """Poll every team box's SSH reachability THROUGH the gateway before the DNS/harden loops.

    Replaces a blind post-clone sleep. Uses one shared time budget across all boxes (they boot
    together, so once cloud-init finishes they come up nearly at once). Individual timeouts just
    warn and proceed — the fix_dns/setup_auth/nakon steps that follow already retry on their
    own — but if EVERY box in the competition times out, that's not "some boxes are slow," it's
    a systemic problem (broken template, dead bridge, ...), and ploughing ahead just repeats the
    same failure through several more 8-attempt retry loops for 30-60+ minutes with nothing to
    show for it. Raises in that all-fail case so deploy()'s phase handler reports it plainly
    instead.
    """
    node = os.environ["TF_VAR_proxmox_node"]
    print("  Waiting for team boxes to accept SSH via gateway...")
    deadline = time.time() + timeout
    total = 0
    unreachable = 0
    for team in teams.values():
        for box_idx, box in enumerate(boxes):
            total += 1
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            while True:
                # Check the shared deadline BEFORE attempting: an earlier box burning the whole
                # budget used to still let every later box make one full (up to 20s) SSH
                # attempt after the deadline had already passed, so "not reachable" was reported
                # without any real probes ever happening for those boxes.
                if time.time() > deadline:
                    print(f"    WARNING: {ip} not reachable within timeout — continuing")
                    vmid = vm_id_for(team["identifier"], box_idx)
                    print(diagnose_unreachable_box(node, vmid))
                    unreachable += 1
                    break
                try:
                    r = ssh_via_gateway(ctx, ip, "true", timeout=20)
                    if r.returncode == 0:
                        print(f"    {ip} reachable")
                        break
                except Exception:
                    pass
                time.sleep(10)

    if total > 0 and unreachable == total:
        raise RuntimeError(
            f"All {total} team box(es) failed to become SSH-reachable — this looks systemic "
            f"(see the guest-agent diagnosis above for each box), not a one-off timing fluke. "
            f"Aborting rather than burning through the DNS/auth/nakon retry loops for boxes "
            f"that are already known unreachable."
        )


def wait_for_cloud_init(ctx, teams, boxes, timeout=240):
    """Block until cloud-init has actually FINISHED on every team box — not just SSH-reachable.

    wait_for_boxes_ssh() only confirms SSH accepts a command; cloud-init can still be mid-flight
    at that point, especially on a freshly cloned box. clone_team_boxes() runs `cloud-init clean
    --machine-id` on team1 before cloning and gives each clone a new ipconfig0/net0, so every
    clone (and the restarted team1) looks like a brand-new instance to cloud-init and reruns its
    full first-boot module set — including the module that secures /etc/sudoers.d permissions
    for the ciuser. If the final run_nakon() pass (which (re)plants filesystem-permission
    misconfigs like writable-sudoers) lands before that module finishes, cloud-init silently
    reverts the plant afterward. Confirmed live: writable-sudoers survived on team1 but a clone
    of the same box came back at safe 750. `cloud-init status --wait` blocks until cloud-init is
    fully done and needs no sudo, closing that race before nakon's no-`--only` pass runs.

    Never raises — a box still mid-boot just gets a warning; the DNS/auth/nakon steps that
    follow already retry on their own, and a box that's SSH-reachable but never finishes
    cloud-init is unusual enough to be worth a loud warning rather than aborting the whole run.
    """
    print("  Waiting for cloud-init to finish on all team boxes...")
    deadline = time.time() + timeout
    for team in teams.values():
        for box in boxes:
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            remaining = max(int(deadline - time.time()), 15)
            try:
                r = ssh_via_gateway(ctx, ip, "cloud-init status --wait", timeout=remaining)
                if r.returncode in (0, 2):  # 2 = done, with non-fatal warnings — still finished
                    print(f"    {ip}: cloud-init done (rc={r.returncode})")
                else:
                    print(f"  WARNING: {ip} cloud-init status --wait exited {r.returncode}: "
                          f"{(r.stdout or '').strip()[:150]}")
            except subprocess.TimeoutExpired:
                print(f"  WARNING: {ip} cloud-init still running after {remaining}s — continuing anyway")
            except Exception as e:
                print(f"  WARNING: cloud-init wait failed for {ip}: {e}")


def wait_for_http(url, timeout=120):
    """Poll a URL until it returns ANY HTTP response (not connection-refused), replacing a
    blind sleep before seeding. requests.get returns for 4xx/5xx too (we don't raise_for_status),
    so any served response means the app is up. Never raises — timeout warns and proceeds.
    """
    print(f"  Waiting for {url} to respond (timeout {timeout}s)...")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            requests.get(url, timeout=5)
            print(f"    {url} responded (after {attempt} attempt(s))")
            return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(3)
    print(f"  WARNING: {url} did not respond within {timeout}s — continuing anyway")
    return False


def fix_dns_on_boxes(teams, boxes, ctx):
    """Fix DNS on every team box. This is the only readiness gate team1 gets in phase [5/7]
    (wait_for_boxes_ssh only runs later, inside clone_team_boxes, for team2+) — so on an all-fail
    run it's this function's circuit breaker, not wait_for_boxes_ssh's, that has to catch it.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    node = os.environ["TF_VAR_proxmox_node"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )
    # Terraform's Step D already did this once before nakon ran; it has to happen again here
    # because clone_team_boxes()' `cloud-init clean` + reboot regenerates resolv.conf.
    print("  Fixing DNS on all team boxes...")
    total = 0
    failed = 0
    for team in teams.values():
        for box_idx, box in enumerate(boxes):
            total += 1
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            for attempt in range(1, 9):
                try:
                    subprocess.run(
                        [
                            "ssh", "-i", key,
                            "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            "-o", "ConnectTimeout=10",
                            "-o", f"ProxyCommand={proxy}",
                            f"ubuntu@{ip}", DNS_FIX_CMD,
                        ],
                        check=True, timeout=40,
                    )
                    print(f"    DNS fixed on {ip}")
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    if attempt < 8:
                        print(f"    DNS fix attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                        time.sleep(15)
                    else:
                        print(f"  WARNING: DNS fix failed for {ip} after 8 attempts — proceeding anyway")
                        vmid = vm_id_for(team["identifier"], box_idx)
                        print(diagnose_unreachable_box(node, vmid))
                        failed += 1

    if total > 0 and failed == total:
        raise RuntimeError(
            f"DNS fix failed on all {total} team box(es) after 8 attempts each — this looks "
            f"systemic (see the guest-agent diagnosis above for each box), not a one-off timing "
            f"fluke. Aborting rather than proceeding into Nakon against boxes that are already "
            f"known unreachable."
        )


def fix_services_on_boxes(comp_dir, teams, boxes, ctx, box_creds=None):
    """Post-Nakon service hardening: make services accessible externally.

    Writes a single hardening script to the gateway, then distributes it to each target box.
    Handles: mysql/mariadb bind address, postfix, nginx, vsftpd, dovecot, bind9.
    Also starts all services on every box.

    box_creds ({"admin": ..., "user1": ..., "user2": ...}) names the credlist accounts created
    on every box — generated fresh per competition in deploy() and must match what
    push_event_conf() writes to linux.credlist, or every credlist-based check scores a healthy
    box as down. Falls back to the legacy fixed literals only if not supplied.
    """
    import base64

    creds = box_creds or {"admin": "changeme123", "user1": "password1", "user2": "password2"}

    node = os.environ["TF_VAR_proxmox_node"]
    print("  Hardening services on all team boxes...")
    box_services = json.loads((comp_dir / "box_services.json").read_text())

    # Build a map of which services each box should run
    box_service_map = {}
    for box in boxes:
        box_service_map[box["name"]] = box_services.get(box["name"], [])

    for team in teams.values():
        for box_idx, box in enumerate(boxes):
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            services = box_service_map.get(box["name"], [])

            # Build a per-box hardening script
            script_lines = ["#!/bin/bash", "set -e", ""]

            # Always create the credlist OS accounts. Quotient's Ssh/Smtp/Imap/Ftp login checks
            # authenticate against these system users; previously they were only created in the
            # postfix branch, so a box that ran ssh (or ftp/imap) but not postfix had no account
            # to log in as and scored permanently down. Creating them unconditionally is cheap
            # and idempotent, and matches the linux.credlist push_event_conf() writes.
            script_lines.extend([
                "# Credlist OS accounts (admin/user1/user2) for all auth-based service checks",
                "sudo useradd -m -s /bin/bash admin 2>/dev/null || true",
                f"echo 'admin:{creds['admin']}' | sudo chpasswd",
                "sudo useradd -m -s /bin/bash user1 2>/dev/null || true",
                f"echo 'user1:{creds['user1']}' | sudo chpasswd",
                "sudo useradd -m -s /bin/bash user2 2>/dev/null || true",
                f"echo 'user2:{creds['user2']}' | sudo chpasswd",
                "",
            ])

            if "mysql" in services or "mariadb" in services:
                script_lines.extend([
                    "# MySQL/MariaDB: bind to 0.0.0.0",
                    "# Try all possible config file locations",
                    "for cnf in /etc/mysql/mysql.conf.d/50-server.cnf /etc/mysql/mariadb.conf.d/50-server.cnf /etc/mysql/my.cnf; do",
                    '  if [ -f "$cnf" ]; then',
                    "    sudo sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' \"$cnf\"",
                    "  fi",
                    "done",
                    "sudo systemctl restart mysql 2>/dev/null || sudo systemctl restart mariadb 2>/dev/null || true",
                    "sleep 2",
                    "# Create MySQL users from credlist",
                    "cat > /tmp/setup_mysql.sql << 'SQLEOF'",
                    f"CREATE USER IF NOT EXISTS 'admin'@'%' IDENTIFIED BY '{creds['admin']}';",
                    "GRANT ALL PRIVILEGES ON *.* TO 'admin'@'%' WITH GRANT OPTION;",
                    f"CREATE USER IF NOT EXISTS 'user1'@'%' IDENTIFIED BY '{creds['user1']}';",
                    "GRANT ALL PRIVILEGES ON *.* TO 'user1'@'%';",
                    f"CREATE USER IF NOT EXISTS 'user2'@'%' IDENTIFIED BY '{creds['user2']}';",
                    "GRANT ALL PRIVILEGES ON *.* TO 'user2'@'%';",
                    "FLUSH PRIVILEGES;",
                    "SQLEOF",
                    "sudo mysql < /tmp/setup_mysql.sql || true",
                    "",
                ])

            if "postfix" in services or "smtp" in services:
                script_lines.extend([
                    "# Postfix: ensure it listens on all interfaces",
                    "sudo postconf -e 'inet_interfaces = all' 2>/dev/null || true",
                    "sudo postconf -e 'inet_protocols = ipv4' 2>/dev/null || true",
                    "# Ensure the smtpd listener exists. A non-interactive postfix install can leave"
                    " master.cf empty (no 'smtp inet' service), so postfix runs but binds nothing on"
                    " :25 and the SMTP check scores down. postconf -M adds it idempotently.",
                    "sudo postconf -M 'smtp/inet=smtp inet n - y - - smtpd' 2>/dev/null || true",
                    "sudo systemctl restart postfix 2>/dev/null || true",
                    "sleep 1",
                    "# Create mail users matching credlist for SMTP checks",
                    "sudo useradd -m -s /bin/bash admin 2>/dev/null || true",
                    f"echo 'admin:{creds['admin']}' | sudo chpasswd",
                    "sudo useradd -m -s /bin/bash user1 2>/dev/null || true",
                    f"echo 'user1:{creds['user1']}' | sudo chpasswd",
                    "sudo useradd -m -s /bin/bash user2 2>/dev/null || true",
                    f"echo 'user2:{creds['user2']}' | sudo chpasswd",
                    "",
                ])

            if "nginx" in services or "http" in services or "web" in services:
                script_lines.extend([
                    "# Nginx: ensure it starts and listens on port 80",
                    "sudo systemctl restart nginx 2>/dev/null || true",
                    "sleep 1",
                    "",
                ])

            if "vsftpd" in services or "ftp" in services:
                script_lines.extend([
                    "# Vsftpd: ensure anonymous is off, local users can login",
                    "sudo sed -i 's/^#*anonymous_enable.*/anonymous_enable=NO/' /etc/vsftpd.conf 2>/dev/null || true",
                    "sudo sed -i 's/^#*local_enable.*/local_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
                    "sudo sed -i 's/^#*write_enable.*/write_enable=YES/' /etc/vsftpd.conf 2>/dev/null || true",
                    "sudo systemctl restart vsftpd 2>/dev/null || true",
                    "sleep 1",
                    "",
                ])

            if "dovecot" in services or "imap" in services:
                script_lines.extend([
                    "# Dovecot: enable plaintext auth, create mail dirs",
                    "sudo sed -i 's/^#*disable_plaintext_auth.*/disable_plaintext_auth = no/' /etc/dovecot/conf.d/10-auth.conf 2>/dev/null || true",
                    # Dovecot 2.4 rewrote its settings around named blocks and renamed this one
                    # to a positive-sense key — the old sed above is a silent no-op on 2.4 (the
                    # line it targets no longer exists in 10-auth.conf at all), so a 2.4 box was
                    # left rejecting every plaintext IMAP login Quotient's check attempts
                    # ("cleartext authentication not allowed without SSL/TLS"), confirmed live on
                    # debian13-lite-fix. `auth_allow_cleartext` isn't a recognized key on pre-2.4
                    # dovecot, so this only writes it when the installed version actually is 2.4+.
                    "dovecot --version 2>/dev/null | grep -qE '^(2\\.[4-9]|[3-9]\\.)' && "
                    "sudo bash -c \"echo 'auth_allow_cleartext = yes' > "
                    "/etc/dovecot/conf.d/99-allow-plaintext.conf\" || true",
                    "sudo mkdir -p /home/user1/mail /home/user2/mail",
                    "sudo chmod 700 /home/user1/mail /home/user2/mail",
                    "sudo chown user1:user1 /home/user1/mail 2>/dev/null || true",
                    "sudo chown user2:user2 /home/user2/mail 2>/dev/null || true",
                    "sudo systemctl restart dovecot 2>/dev/null || true",
                    "sleep 1",
                    "",
                ])

            if "bind" in services or "named" in services or "dns" in services:
                script_lines.extend([
                    "# Bind9: allow queries from anywhere",
                    "cat > /tmp/named.conf.options << 'BIND9EOF'",
                    "options {",
                    '  directory "/var/cache/bind";',
                    "  recursion yes;",
                    "  allow-query { any; };",
                    "  forwarders { 8.8.8.8; 1.1.1.1; };",
                    "};",
                    "BIND9EOF",
                    "sudo cp /tmp/named.conf.options /etc/bind/named.conf.options",
                    # Fix missing named.conf.default-zones
                    "if [ ! -f /etc/bind/named.conf.default-zones ]; then",
                    "  cat > /tmp/named.conf.default-zones << 'BZEOF'",
                    'zone "." {',
                    "  type hint;",
                    '  file "/usr/share/dns/root.hints";',
                    "};",
                    'zone "localhost" {',
                    "  type master;",
                    '  file "/etc/bind/db.local";',
                    "};",
                    'zone "127.in-addr.arpa" {',
                    "  type master;",
                    '  file "/etc/bind/db.127";',
                    "};",
                    "BZEOF",
                    "  sudo cp /tmp/named.conf.default-zones /etc/bind/named.conf.default-zones",
                    "fi",
                    # Ensure db.local exists (some templates strip it)
                    "if [ ! -f /etc/bind/db.local ]; then",
                    "  cat > /tmp/db.local << 'DLEOF'",
                    '$TTL 86400',
                    '@   IN  SOA ns1.localhost. root.localhost. (',
                    '        2026071201',
                    '        3600',
                    '        1800',
                    '        604800',
                    '        86400 )',
                    '    IN  NS  ns1.localhost.',
                    'ns1 IN  A   127.0.0.1',
                    '@   IN  A   127.0.0.1',
                    "DLEOF",
                    "  sudo cp /tmp/db.local /etc/bind/db.local",
                    "fi",
                    "sudo systemctl restart bind9 2>/dev/null || true",
                    "sleep 1",
                    "",
                ])

            if "telnet-service" in services or "telnet" in services:
                script_lines.extend([
                    "# Telnet: Debian ships the inetd entry disabled ('#<off>#' in"
                    " /etc/inetd.conf), and inetutils-inetd's ExecCondition refuses to even"
                    " start while every entry is off — nakon's install alone leaves nothing"
                    " listening on :23.",
                    "sudo update-inetd --enable telnet 2>/dev/null || true",
                    "sudo systemctl restart inetutils-inetd 2>/dev/null || true",
                    "sleep 1",
                    "",
                ])

            if "splunk" in services:
                script_lines.extend([
                    "# Splunk: Nakon only creates user, need something on port 8000",
                    "sudo apt-get install -y lighttpd 2>/dev/null || true",
                    "sudo sed -i 's/server.port.*/server.port = 8000/' /etc/lighttpd/lighttpd.conf 2>/dev/null || true",
                    "sudo systemctl enable lighttpd 2>/dev/null || true",
                    "sudo systemctl start lighttpd 2>/dev/null || true",
                    "sleep 1",
                    "",
                ])

            # Always ensure all relevant services are started
            script_lines.extend([
                "# Ensure all installed services are running",
                "for svc in mysql mariadb postfix nginx vsftpd dovecot bind9 lighttpd apache2; do",
                "  if systemctl list-unit-files \"$svc.service\" &>/dev/null; then",
                "    sudo systemctl start $svc 2>/dev/null || true",
                "  fi",
                "done",
            ])

            if len(script_lines) <= 3:  # Only shebang, set -e, and empty line
                print(f"    No hardening needed on {ip}")
                continue

            # Encode script as base64 to avoid ALL quoting issues
            script_content = "\n".join(script_lines)
            script_b64 = base64.b64encode(script_content.encode()).decode()

            # Deploy and execute via gateway
            deploy_cmd = (
                f"echo '{script_b64}' | base64 -d > /tmp/harden.sh && "
                "chmod +x /tmp/harden.sh && "
                "bash /tmp/harden.sh"
            )

            try:
                result = ssh_via_gateway(ctx, ip, deploy_cmd, timeout=60)
                if result.returncode != 0:
                    # A box whose randomly-picked vulns include writable-sudoers can end up with
                    # /etc/sudoers.d world-writable, which makes modern sudo refuse to trust
                    # ANY rule there — including sudo-nopasswd's NOPASSWD line — and demand a
                    # real password the key-only ubuntu user doesn't have. Every `sudo ...` line
                    # in this script then fails ("a password is required"), so nothing after it
                    # runs (the script has `set -e`): no credlist accounts, no service tweaks.
                    # Fall back to the QEMU guest agent, which executes as root directly and so
                    # needs no sudo at all — this repairs OUR provisioning without touching the
                    # box's sudoers permissions, so the vuln stays intact for whoever's meant to
                    # find it.
                    print(f"    Service hardening warning on {ip}: {result.stderr.strip()[:200]}")
                    print(f"    Retrying {ip} via guest agent (as root, no sudo needed)...")
                    vmid = vm_id_for(team["identifier"], box_idx)
                    # \b (not ^\s*) so this also catches mid-pipeline uses like
                    # "echo ... | sudo chpasswd", not just line-leading ones.
                    root_script = re.sub(r"\bsudo ", "", script_content)
                    try:
                        rc, out, err = guest_agent_exec_root(node, vmid, root_script, timeout=120)
                        if rc == 0:
                            print(f"    Services hardened on {ip} (via guest agent)")
                        else:
                            print(f"    Service hardening still failing on {ip} via guest agent: "
                                  f"rc={rc} {err.strip()[:200]}")
                    except Exception as e:
                        print(f"    Guest-agent fallback failed for {ip} (vmid {vmid}): {e}")
                else:
                    print(f"    Services hardened on {ip}")
            except Exception as e:
                print(f"    Service hardening error on {ip}: {e}")


def setup_ubuntu_auth(teams, boxes, ctx):
    """Enable password auth and NOPASSWD sudo for ubuntu on team boxes.

    The template has PasswordAuthentication disabled, but Nakon's paramiko connections use
    password auth (the per-competition box_password set via cloud-init/var.box_password, not
    a fixed literal). Also, Nakon runs sudo commands to install packages, so NOPASSWD is
    required. This function itself authenticates by SSH key, not password, so it's unaffected
    by box_password's value.

    Runs via the scoring engine gateway since team boxes are on isolated bridges.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    proxy = (
        f"ssh -i {key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        f"-W %h:%p {scoring_user}@{scoring_ip}"
    )

    print("  Enabling password auth + NOPASSWD sudo for ubuntu on team boxes...")
    auth_cmd = (
        "sudo sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' /etc/ssh/sshd_config; "
        "sudo systemctl restart sshd 2>/dev/null || true; "
        "echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ubuntu; "
        "sudo chmod 440 /etc/sudoers.d/ubuntu"
    )

    for team in teams.values():
        for box in boxes:
            ip = f"192.168.{team['identifier']}.{box['last_octet']}"
            for attempt in range(1, 9):
                try:
                    subprocess.run(
                        [
                            "ssh", "-i", key,
                            "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            "-o", "ConnectTimeout=10",
                            "-o", f"ProxyCommand={proxy}",
                            f"ubuntu@{ip}", auth_cmd,
                        ],
                        check=True, timeout=40,
                    )
                    print(f"    Auth configured on {ip}")
                    break
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    if attempt < 8:
                        print(f"    Auth attempt {attempt}/8 failed for {ip}, retrying in 15s...")
                        time.sleep(15)
                    else:
                        print(f"  WARNING: Auth setup failed for {ip} after 8 attempts — proceeding anyway")


def clone_team_boxes(teams, boxes, ctx, comp_dir, box_creds=None):
    """Clone team1 boxes to other teams and configure networking.

    After cloning, reconfigures IP addresses, fixes DNS, and hardens services
    on ALL team boxes (team1 + cloned teams).
    """
    node = os.environ["TF_VAR_proxmox_node"]
    team_ids = list(teams.values())
    if len(team_ids) < 2:
        print("  Only one team — skipping box cloning.")
        return

    team1 = team_ids[0]

    # Step 1: Run cloud-init clean on team1 boxes
    print("  Running cloud-init clean on team1 boxes...")
    for box in boxes:
        ip = f"192.168.{team1['identifier']}.{box['last_octet']}"
        try:
            result = ssh_via_gateway(ctx, ip, "sudo cloud-init clean --logs --machine-id", timeout=30)
            print(f"    {box['name']}: cloud-init clean done")
        except Exception as e:
            print(f"  WARNING: cloud-init clean failed for {box['name']}: {e}")

    # Step 2: Stop team1 boxes
    # Use 0-based box index for vm_id_for (Terraform creates VMs with 0-based index)
    # while last_octet is used for IP addresses
    print("  Shutting down team1 boxes...")
    for box_idx, box in enumerate(boxes):
        vmid = vm_id_for(team1["identifier"], box_idx)
        vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
        vm = next((v for v in vms if v["vmid"] == vmid), None)
        if vm and vm.get("status") == "running":
            upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/stop")["data"]
            wait_for_proxmox_task(node, upid)
        print(f"    team1-{box['name']} (vmid {vmid}) stopped")

    # Step 3: Clone team1 boxes for each subsequent team
    print("  Cloning team1 boxes to other teams...")
    # Record cloned VM ids so destroy-competition.py can tear them down — these are
    # created directly via the Proxmox API, so they're NOT in Terraform state and
    # `terraform destroy` won't remove them. Loaded (not reset) and written after every clone
    # — not just once at the end — so a crash partway through this loop doesn't lose the record
    # of what's already been created, and a `--from-phase 6` resume doesn't orphan it.
    cloned_vms_path = comp_dir / "cloned_vms.json"
    cloned_vms = json.loads(cloned_vms_path.read_text()) if cloned_vms_path.exists() else {}
    # Existing VMIDs on Proxmox — lets a resume after a partial clone failure skip boxes a prior
    # run already cloned, instead of re-cloning into a vmid that's already taken and looping on
    # a "VM already exists" error.
    existing_vmids = {v["vmid"] for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
    for team in team_ids[1:]:
        for box_idx, box in enumerate(boxes):
            src_vmid = vm_id_for(team1["identifier"], box_idx)
            dst_vmid = vm_id_for(team["identifier"], box_idx)
            clone_name = f"{team['identifier']}-{box['name']}"

            if dst_vmid in existing_vmids:
                print(f"    {clone_name} (vmid {dst_vmid}) already exists — skipping clone (resume)")
            else:
                upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src_vmid}/clone", data={
                    "newid": dst_vmid,
                    "name": clone_name,
                    "full": 1,
                })["data"]
                print(f"    team1-{box['name']} (vmid {src_vmid}) -> {clone_name} (vmid {dst_vmid})...")
                wait_for_proxmox_task(node, upid)

            cloned_vms[clone_name] = dst_vmid
            cloned_vms_path.write_text(json.dumps(cloned_vms, indent=2))

            # Fix cloud-init IP for the cloned VM (last_octet is correct for IPs). Reapplied
            # even when the clone itself was skipped above — this PUT is idempotent, and a
            # prior run could have crashed between the clone and this step.
            team_subnet = team["identifier"]
            box_octet = box["last_octet"]
            ipconfig = f"ip=192.168.{team_subnet}.{box_octet}/24,gw=192.168.{team_subnet}.1"
            bridge = f"vmbr{team_subnet}"
            # `ipconfig0` is the cloud-init IP key; there is no `ciipconfig0` param and
            # including it makes Proxmox reject the whole request (400 Parameter verification
            # failed). Let Proxmox auto-assign the NIC MAC rather than pinning 00:00:00:00:00:00.
            proxmox_api("PUT", f"/nodes/{node}/qemu/{dst_vmid}/config", data={
                "ipconfig0": ipconfig,
                "net0": f"virtio,bridge={bridge}",
            })

    # Step 4: Start ALL team boxes (team1 + cloned)
    print("  Starting all team boxes...")
    for team in team_ids:
        for box_idx, box in enumerate(boxes):
            vmid = vm_id_for(team["identifier"], box_idx)
            try:
                vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
                vm = next((v for v in vms if v["vmid"] == vmid), None)
                if vm and vm.get("status") != "running":
                    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{vmid}/status/start")["data"]
                    wait_for_proxmox_task(node, upid, timeout=120)
                print(f"    vmid {vmid} ({team['identifier']}-{box['name']}) started")
            except Exception as e:
                print(f"  WARNING: Failed to start vmid {vmid}: {e}")

    # Wait for SSH first (cheap, gates the boxes being up at all), then for cloud-init to
    # actually finish (closes the race with nakon's final plant of filesystem-permission
    # misconfigs — see wait_for_cloud_init()'s docstring). Poll each box THROUGH the gateway
    # instead of a blind sleep; the fix_dns/auth loops below still retry on their own.
    wait_for_boxes_ssh(ctx, teams, boxes, timeout=300)
    wait_for_cloud_init(ctx, teams, boxes, timeout=240)

    # Step 5: Fix DNS on ALL team boxes
    fix_dns_on_boxes(teams, boxes, ctx)

    # Step 6: Setup ubuntu auth on ALL team boxes (cloned VMs may need it reset)
    setup_ubuntu_auth(teams, boxes, ctx)

    # Step 7: Harden services on ALL team boxes
    fix_services_on_boxes(comp_dir, teams, boxes, ctx, box_creds=box_creds)


def bootstrap_scoring_engine(ctx, postgres_password, redis_password):
    """Bootstrap the scoring engine: install packages, Docker, Quotient.

    postgres_password / redis_password are generated once per deploy() run and passed in so the
    Quotient stack .env written here agrees with the same values push_event_conf() writes later.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    print("  Installing packages on scoring engine...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "sudo killall apt-get apt dpkg 2>/dev/null; sleep 2; "
            "sudo rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock 2>/dev/null; "
            "sudo dpkg --configure -a 2>/dev/null; "
            "sudo apt-get update && sudo apt-get install -y docker.io git curl python3-pip",
        ],
        check=True, timeout=180,
    )

    print("  Installing Docker Compose v2 plugin...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                "install -m 0755 -d /etc/apt/keyrings && "
                "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc > /dev/null && "
                "sudo chmod a+r /etc/apt/keyrings/docker.asc && "
                "echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] "
                "https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable\" "
                "| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null && "
                "sudo apt-get update && sudo apt-get install -y docker-compose-plugin"
            ),
        ],
        check=True, timeout=180,
    )

    print("  Starting Docker (already installed by Terraform)...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "sudo systemctl start docker && sudo systemctl enable docker && sleep 2 && sudo docker version",
        ],
        check=True, timeout=30,
    )

    print("  Cloning Quotient...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "sudo mkdir -p /opt/quotient && sudo git clone --depth 1 --recurse-submodules https://github.com/dbaseqp/Quotient.git /opt/quotient 2>/dev/null || (cd /opt/quotient && sudo git submodule update --init --recursive)",
        ],
        check=True, timeout=60,
    )

    # Write .env for Quotient (required before docker compose build/up)
    print("  Writing Quotient .env...")
    import base64
    quotient_env = (
        f"POSTGRES_PASSWORD={postgres_password}\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=quotient_database\n"
        "POSTGRES_DB=engine\n"
        f"REDIS_PASSWORD={redis_password}\n"
    )
    env_b64 = base64.b64encode(quotient_env.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env",
        ],
        check=True, timeout=10,
    )

    print("  Building Quotient Docker images...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose build --no-cache",
        ],
        check=True, timeout=1800,
    )

    print("  Starting Quotient Docker containers...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose up -d",
        ],
        check=True, timeout=60,
    )

    # CRITICAL: Docker sets FORWARD policy to DROP on start. Restore forwarding rules
    # so the scoring engine can route between team subnets and the internet.
    # Also enable TCP forwarding on sshd for ProxyCommand tunneling.
    print("  Restoring network forwarding rules after Docker start...")
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                # Allow all forwarding within team subnets and NAT to internet
                "sudo iptables -P FORWARD ACCEPT && "
                # Block team-to-team traffic. Teams can only reach each other THROUGH the
                # engine (each team bridge has no uplink of its own, but the engine has a NIC
                # on every one and forwards between them) — without this DROP rule, the
                # bridges' lack of a physical uplink isolates nothing. -I ... 1 so this can't
                # be shadowed by anything Docker inserts ahead of it in FORWARD on a restart.
                "sudo iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
                "sudo iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP && "
                "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE && "
                # Enable TCP forwarding for ProxyCommand tunnels
                "sudo sed -i 's/^#*AllowTcpForwarding.*/AllowTcpForwarding yes/' /etc/ssh/sshd_config && "
                "sudo systemctl reload sshd 2>/dev/null || true"
            ),
        ],
        check=True, timeout=15,
    )
    print("  Forwarding rules restored")

    # Make the team-subnet NAT *and* the team-isolation DROP rule durable. Both get wiped every
    # time Docker re-syncs iptables (any container start/restart), silently cutting team boxes
    # off the internet AND, worse, silently re-opening team-to-team routing. ensure_nat_forwarding()
    # only re-asserts before nakon runs — not good enough once the range is live. Install a tiny
    # idempotent systemd oneshot + a 30s timer ON THE ENGINE so both rules are continuously
    # re-asserted for the lifetime of the range. This is the range-firewall.sh/.service/.timer
    # referenced in docs/usage-people.md's Troubleshooting table.
    print("  Installing range-firewall systemd unit + timer (keeps team NAT + isolation durable)...")
    firewall_script = (
        "#!/bin/bash\n"
        "iptables -P FORWARD ACCEPT\n"
        # Team-to-team isolation: the engine has a NIC on every team bridge (needed to NAT them
        # out), so without this DROP rule any team can route straight to any other team's boxes
        # through it. -I ... 1 keeps it ahead of anything Docker inserts into FORWARD.
        "iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
        "iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP\n"
        "iptables -t nat -C POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE 2>/dev/null || "
        "iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE\n"
    )
    firewall_service = (
        "[Unit]\n"
        "Description=Re-assert team NAT/forwarding + team-to-team isolation (Docker wipes it on restart)\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/sbin/range-firewall.sh\n"
    )
    firewall_timer = (
        "[Unit]\n"
        "Description=Periodically re-assert team NAT/forwarding + team-to-team isolation\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=30\n"
        "OnUnitActiveSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    firewall_script_b64 = base64.b64encode(firewall_script.encode()).decode()
    firewall_service_b64 = base64.b64encode(firewall_service.encode()).decode()
    firewall_timer_b64 = base64.b64encode(firewall_timer.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                f"echo '{firewall_script_b64}' | base64 -d | sudo tee /usr/local/sbin/range-firewall.sh > /dev/null && "
                "sudo chmod +x /usr/local/sbin/range-firewall.sh && "
                f"echo '{firewall_service_b64}' | base64 -d | sudo tee /etc/systemd/system/range-firewall.service > /dev/null && "
                f"echo '{firewall_timer_b64}' | base64 -d | sudo tee /etc/systemd/system/range-firewall.timer > /dev/null && "
                "sudo systemctl daemon-reload && sudo systemctl enable --now range-firewall.timer"
            ),
        ],
        check=True, timeout=30,
    )
    print("  range-firewall.timer enabled (re-asserts NAT + isolation every 30s)")

    install_range_healthcheck(ctx)

    # Team bridge NICs (ens19/ens20/...) are fully configured by Terraform's
    # null_resource.team_nics + null_resource.reboot_scoring_engine (see terraform/main.tf) by
    # the time this phase runs — no separate configuration needed here.


def install_range_healthcheck(ctx):
    """Install a lightweight, zero-dependency live-ops health check on the scoring engine.

    Nothing in this tool watches a *running* competition — range-firewall.timer self-heals the
    NAT/isolation rules but logs nothing and alerts no one, and verify-competition.py is a
    manual one-shot script. This adds a lightweight systemd timer (same push pattern as
    range-firewall above) that checks Quotient's container + API, and the NAT/isolation rules,
    every 60s, and appends one line per FAILURE to /var/log/range-healthcheck.log (silent when
    healthy, so `tail -f` during an event only ever shows something an operator needs to act
    on). Deliberately does NOT push-alert (email/Slack/webhook) — that's a bigger, separate
    call for an operator to wire in explicitly; see docs/usage-people.md's Troubleshooting
    table for the log-watching workflow this is meant to support.
    """
    import base64

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    print("  Installing range-healthcheck systemd unit + timer...")
    healthcheck_script = (
        "#!/bin/bash\n"
        "LOG=/var/log/range-healthcheck.log\n"
        "ts() { date '+%Y-%m-%d %H:%M:%S'; }\n"
        "\n"
        "# 1. Quotient container(s) running\n"
        "if ! docker ps --filter 'name=quotient' --filter 'status=running' -q | grep -q .; then\n"
        "  echo \"$(ts) FAIL quotient-containers: no running container matching name=quotient\" >> \"$LOG\"\n"
        "fi\n"
        "\n"
        "# 2. Quotient's API is responding at all. Deliberately NOT curl -f: /api/login is a\n"
        "# POST-only route, so a plain GET correctly gets 405 -- that's still proof the server\n"
        "# is up. Only '000' (curl's code for no connection at all) counts as down.\n"
        "code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost/api/login 2>/dev/null)\n"
        "if [ -z \"$code\" ] || [ \"$code\" = '000' ]; then\n"
        "  echo \"$(ts) FAIL quotient-api: http://localhost/api/login did not respond (curl code: ${code:-none})\" >> \"$LOG\"\n"
        "fi\n"
        "\n"
        "# 3. Team-to-team isolation rule present (see bootstrap_scoring_engine()/range-firewall.sh)\n"
        "if ! iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null; then\n"
        "  echo \"$(ts) FAIL isolation-rule: team-to-team DROP rule missing from FORWARD -- "
        "teams may be able to reach each other right now\" >> \"$LOG\"\n"
        "fi\n"
        "\n"
        "# 4. Team-subnet NAT rule present (nakon/team boxes need this for internet access)\n"
        "if ! iptables -t nat -C POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE 2>/dev/null; then\n"
        "  echo \"$(ts) FAIL nat-rule: team-subnet MASQUERADE missing from POSTROUTING\" >> \"$LOG\"\n"
        "fi\n"
    )
    healthcheck_service = (
        "[Unit]\n"
        "Description=Range live-ops health check (Quotient + NAT + isolation)\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/sbin/range-healthcheck.sh\n"
    )
    healthcheck_timer = (
        "[Unit]\n"
        "Description=Periodically run the range live-ops health check\n"
        "\n"
        "[Timer]\n"
        # Offset from range-firewall.timer's :00/:30-second cadence so the two don't fire in
        # the same tick under systemd's default randomization-free scheduling.
        "OnBootSec=60\n"
        "OnUnitActiveSec=60\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    healthcheck_script_b64 = base64.b64encode(healthcheck_script.encode()).decode()
    healthcheck_service_b64 = base64.b64encode(healthcheck_service.encode()).decode()
    healthcheck_timer_b64 = base64.b64encode(healthcheck_timer.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            (
                f"echo '{healthcheck_script_b64}' | base64 -d | sudo tee /usr/local/sbin/range-healthcheck.sh > /dev/null && "
                "sudo chmod +x /usr/local/sbin/range-healthcheck.sh && "
                f"echo '{healthcheck_service_b64}' | base64 -d | sudo tee /etc/systemd/system/range-healthcheck.service > /dev/null && "
                f"echo '{healthcheck_timer_b64}' | base64 -d | sudo tee /etc/systemd/system/range-healthcheck.timer > /dev/null && "
                "sudo touch /var/log/range-healthcheck.log && "
                "sudo systemctl daemon-reload && sudo systemctl enable --now range-healthcheck.timer"
            ),
        ],
        check=True, timeout=30,
    )
    print("  range-healthcheck.timer enabled (checks every 60s, logs failures only)")


def ensure_nat_forwarding(ctx):
    """Idempotently (re)assert the engine's team-subnet NAT + forwarding + team isolation.

    The scoring engine is every team's NAT gateway to the internet, which nakon needs for
    apt-get. But Docker re-syncs iptables on any container start/restart and drops the custom
    team-subnet MASQUERADE, leaving boxes offline — and nakon swallows the resulting apt
    failures, so services silently don't install. The same resync also drops the team-to-team
    DROP rule (range-firewall.timer re-asserts both every 30s once the range is live, but that
    timer isn't installed until later in bootstrap_scoring_engine() — this call is what covers
    the gap during phases 4-6, before nakon runs). Call this right before any step that needs
    the team boxes online (i.e. before each nakon run). Only the boxes→internet path needs
    NAT; engine→box scoring is direct routing on the team bridge, so this is only about nakon.
    """
    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]
    cmd = (
        "sudo iptables -P FORWARD ACCEPT; "
        "sudo iptables -C FORWARD -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP 2>/dev/null || "
        "sudo iptables -I FORWARD 1 -s 192.168.0.0/16 -d 192.168.0.0/16 -j DROP; "
        "sudo iptables -t nat -C POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE 2>/dev/null || "
        "sudo iptables -t nat -A POSTROUTING -s 192.168.0.0/16 ! -d 192.168.0.0/16 -j MASQUERADE"
    )
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}", cmd,
        ],
        check=False, timeout=30,
    )
    print("  NAT/forwarding ensured on scoring engine")


def push_event_conf(comp_dir, teams, boxes, ctx, event_name, inject_password=None,
                    admin_password="changeme123", postgres_password="postgres_password",
                    redis_password="redis_password", box_creds=None):
    """Build event.conf and push it to the scoring engine.

    postgres_password / redis_password come from deploy() so the .env rewritten here matches the
    one bootstrap_scoring_engine() wrote — Postgres and the app must agree on the same secret.

    box_creds ({"admin": ..., "user1": ..., "user2": ...}) is the credlist content Quotient's
    Ssh/Smtp/Imap/Sql/Ftp checks authenticate WITH — it has to name accounts that really exist
    on the boxes, i.e. exactly what fix_services_on_boxes() creates there. Falls back to the
    legacy fixed literals only if not supplied (keeps this callable standalone / from tests).
    """
    import base64

    key = ctx["ssh_key_path"]
    scoring_ip = ctx["scoring_engine_ip"]
    scoring_user = ctx["vm_username"]

    box_services = json.loads((comp_dir / "box_services.json").read_text())

    # Build the context dict that build_event_conf expects
    quotient_ctx = {
        "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
        "boxes_per_team": boxes,
        "team_passwords": {team_key: team_data["password"] for team_key, team_data in teams.items()},
        "event_name": event_name,
        "quotient_admin_password": admin_password,
        "inject_password": inject_password,
    }

    # Build event.conf from box_services
    event_conf = build_event_conf(quotient_ctx, box_services)
    event_conf_toml = toml.dumps(event_conf)

    # Write event.conf to scoring engine
    event_conf_b64 = base64.b64encode(event_conf_toml.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"echo '{event_conf_b64}' | base64 -d | sudo tee /opt/quotient/config/event.conf",
        ],
        check=True, timeout=30,
    )

    # Write credlist. box_creds names the SAME accounts fix_services_on_boxes() creates on every
    # box — the two have to agree or every credlist-based check (Ssh/Smtp/Imap/Sql/Ftp) scores a
    # healthy box as down.
    creds = box_creds or {"admin": "changeme123", "user1": "password1", "user2": "password2"}
    credlist = "".join(f"{user},{pw}\n" for user, pw in creds.items())
    credlist_b64 = base64.b64encode(credlist.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"mkdir -p /opt/quotient/config/credlists && echo '{credlist_b64}' | base64 -d | sudo tee /opt/quotient/config/credlists/linux.credlist",
        ],
        check=True, timeout=30,
    )

    # Write .env for Quotient (matches docker-compose.yml env_file expectations)
    env_content = (
        f"POSTGRES_PASSWORD={postgres_password}\n"
        "POSTGRES_USER=engineuser\n"
        "POSTGRES_HOST=quotient_database\n"
        "POSTGRES_DB=engine\n"
        f"REDIS_PASSWORD={redis_password}\n"
    )
    env_b64 = base64.b64encode(env_content.encode()).decode()
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            f"echo '{env_b64}' | base64 -d | sudo tee /opt/quotient/.env",
        ],
        check=True, timeout=30,
    )

    # Restart Quotient to pick up new config
    subprocess.run(
        [
            "ssh", "-i", key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{scoring_user}@{scoring_ip}",
            "cd /opt/quotient && sudo docker compose restart",
        ],
        check=True, timeout=60,
    )

    print("  Event configuration pushed to scoring engine")


def deploy(comp_dir, num_teams=None, assume_yes=False, from_phase=1):
    """Main deployment pipeline.

    num_teams / assume_yes let the tool run non-interactively (argparse in main()): when they're
    None/False the function prompts exactly as it did before. from_phase (>1) resumes a partially
    built range: it SKIPS the destructive [1/7] cleanup and [2/7] terraform apply, and reloads the
    teams + per-run secrets from competitions/<id>/.deploy_state.json so a resume agrees with what
    was already deployed. After each numbered phase completes, the last-completed phase number is
    checkpointed to that state file; on failure the resume command is printed.
    """
    # Load competition configuration (Compfile is key=value format)
    name, scenario, difficulty = load_compfile(comp_dir / "Compfile")
    comp_name = comp_dir.name
    print(f"\n{'='*60}")
    print(f"  Deploying {comp_name}")
    print(f"{'='*60}\n")

    boxes = load_boxes(comp_dir)
    if not boxes:
        print("  ERROR: No boxes.json found. Create a new competition or add boxes.json.")
        sys.exit(1)

    # Warn (don't block) if this competition's boxes.json still names a template known to be
    # broken (see docs/usage-people.md's "Adding a template VM" — 106/ubuntu24.04 and
    # 920/debian13-lite have bad cloud-init and clones never get a working network/SSH). A
    # reused competition replays its saved boxes.json exactly, so this can silently outlive the
    # interactive box-picker warning that would otherwise catch it — unconditional here (not
    # gated by --yes/confirm_deploy) since a non-interactive/CI deploy skips that prompt too.
    KNOWN_BROKEN_TEMPLATES = {"debian13-lite", "ubuntu24.04"}
    for b in boxes:
        if b.get("template") in KNOWN_BROKEN_TEMPLATES:
            print(f"  WARNING: box '{b['name']}' uses template '{b['template']}', which is "
                  f"known broken (bad cloud-init — clones won't get a working network/SSH). "
                  f"Use '{b['template']}-fix' instead. See docs/usage-people.md's "
                  f"Troubleshooting table.")

    # Resumable-phase state. Secrets (admin/inject/postgres/redis) and the team set are per-run;
    # a resume MUST reuse the originals or the engine's already-written .env / already-seeded
    # admin login won't match. Persist them here (gitignored, mode 0600) and reload on resume.
    state_path = comp_dir / ".deploy_state.json"
    resuming = from_phase > 1 and state_path.exists()

    def _save_state():
        state_path.write_text(json.dumps(state, indent=2))
        try:
            os.chmod(state_path, 0o600)
        except OSError:
            pass

    def checkpoint(n):
        state["last_phase"] = n
        _save_state()

    injects = load_injects(comp_dir)

    if resuming:
        state = json.loads(state_path.read_text())
        teams = {
            k: {"identifier": v["identifier"], "password": v["password"]}
            for k, v in state["teams"].items()
        }
        number_of_teams = len(teams)
        admin_password = state.get("admin_password") or random_password()
        postgres_password = state.get("postgres_password") or random_password()
        redis_password = state.get("redis_password") or random_password()
        box_password = state.get("box_password") or random_password()
        box_creds = state.get("box_creds") or {
            "admin": random_password(), "user1": random_password(), "user2": random_password(),
        }
        inject_password = state.get("inject_password")
        print(f"  Resuming from phase {from_phase} "
              f"({number_of_teams} team(s), last completed phase {state.get('last_phase')})")
    else:
        if num_teams is not None:
            if not (1 <= num_teams <= MAX_TEAMS):
                raise SystemExit(
                    f"--teams must be between 1 and {MAX_TEAMS} (team identifiers are "
                    f"192.168.<101-254>.x)"
                )
            number_of_teams = num_teams
        else:
            while True:
                raw = input("How many teams? ").strip()
                try:
                    number_of_teams = int(raw)
                except ValueError:
                    print("  Enter a whole number.")
                    continue
                if 1 <= number_of_teams <= MAX_TEAMS:
                    break
                print(f"  Enter a number from 1 to {MAX_TEAMS} "
                      f"(team identifiers are 192.168.<101-254>.x).")
        teams = collect_teams(number_of_teams)
        # Per-competition Quotient web-admin password (scoreboard/admin login only). Postgres and
        # Redis passwords for the Quotient stack are generated once here and passed to both
        # bootstrap_scoring_engine() and push_event_conf() so the two .env writes agree.
        admin_password = random_password()
        postgres_password = random_password()
        redis_password = random_password()
        # Box login (the 'ubuntu' cloud-init account every target box gets) and the credlist
        # accounts Quotient's Ssh/Smtp/Imap/Sql/Ftp checks authenticate WITH (admin/user1/user2)
        # — generated fresh per competition, same as admin/postgres/redis above, instead of the
        # fixed ubuntu/ubuntu + admin/changeme123 literals this used to ship with. A fixed value
        # across every deployment is guessable from this open-source repo, or from fingerprinting
        # a past deploy — see utils.py's BOX_USERNAME/BOX_PASSWORD comment.
        box_password = random_password()
        box_creds = {
            "admin": random_password(), "user1": random_password(), "user2": random_password(),
        }
        inject_password = random_password() if injects else None
        state = {
            "last_phase": 0,
            "teams": teams,
            "admin_password": admin_password,
            "inject_password": inject_password,
            "postgres_password": postgres_password,
            "redis_password": redis_password,
            "box_password": box_password,
            "box_creds": box_creds,
        }
        _save_state()

    # Generate Nakon config
    nakon_config_path = generate_nakon_config(teams, boxes, difficulty, comp_dir, box_password)

    # Build the Nakon bundle here, on the operator machine, from the FULL machine list. This
    # is the only step that needs the vulndb (MySQL + vulndb-ui/MinIO), and
    # generate_nakon_config() above already proves it's reachable from here. Phases 5 and 6
    # then deploy from this one bundle, so the scoring engine never sees vulndb credentials.
    nakon_bundle = build_nakon_bundle(nakon_config_path)

    # Update environment variables for Terraform
    # Terraform reads TF_VAR_teams and TF_VAR_boxes_per_team as JSON strings
    teams_json = json.dumps({
        team_key: {"identifier": team_data["identifier"], "password": team_data["password"]}
        for team_key, team_data in teams.items()
    })
    boxes_json = json.dumps(boxes)

    update_env({
        "TF_VAR_teams": teams_json,
        "TF_VAR_boxes_per_team": boxes_json,
        "TF_VAR_box_password": box_password,
    })

    # Persist team credentials so destroy-competition.py can find and tear down this
    # competition later (it requires teams.json to exist). Also lets us look up team
    # logins for verification without re-deriving them.
    (comp_dir / "teams.json").write_text(teams_json)

    # Confirm deployment (skipped by --yes and on resume — resuming implies prior confirmation)
    if not assume_yes and not resuming:
        if not confirm_deploy(name, scenario, difficulty, teams, boxes):
            print("  Deployment cancelled.")
            return

    node = os.environ["TF_VAR_proxmox_node"]
    # Tracks the phase currently executing so the failure handler can tell the operator exactly
    # where to resume from.
    current_phase = max(from_phase, 1)
    try:
        # [1/7] Clean up previous deployment (DESTRUCTIVE — skipped on resume)
        if from_phase <= 1:
            current_phase = 1
            print("[1/7] Cleaning up previous deployment...")
            for team in teams.values():
                # vm_id_for takes the 0-based box index (matching main.tf and clone_team_boxes),
                # NOT last_octet — passing last_octet computed the wrong vmids, so cleanup never
                # actually removed the previous run's team boxes and they lingered to collide with
                # the next run.
                for box_idx, _box in enumerate(boxes):
                    destroy_vm_if_exists(node, vm_id_for(team["identifier"], box_idx))
            # Destroy scoring engine (vmid hardcoded in main.tf)
            destroy_vm_if_exists(node, 1000)
            # Destroy bridges
            for team in teams.values():
                destroy_bridge_if_exists(node, f"vmbr{team['identifier']}")
            time.sleep(5)
            checkpoint(1)
        else:
            print("[1/7] Skipped (resume) — leaving existing VMs/bridges in place.")

        # [2/7] Terraform init & apply (DESTRUCTIVE rebuild — skipped on resume)
        if from_phase <= 2:
            current_phase = 2
            print("[2/7] Running Terraform init & apply...")
            subprocess.run(["terraform", "init"], cwd="terraform", check=True, timeout=120)
            # -parallelism=1 serializes the clones: full-cloning the scoring engine and every team
            # box at once saturates the datastore and the Proxmox API starts returning HTTP 596
            # (timeout), failing the apply. Cloning one VM at a time is slower but reliable
            # (main.tf assumes this).
            subprocess.run(["terraform", "apply", "-auto-approve", "-parallelism=1"], cwd="terraform", check=True, timeout=2400)

            # Poll the engine's SSH reachability instead of a blind post-apply sleep.
            apply_ctx = read_terraform_ctx()
            wait_for_ssh(apply_ctx["ssh_key_path"], apply_ctx["vm_username"],
                         apply_ctx["scoring_engine_ip"], timeout=300)
            checkpoint(2)
        else:
            print("[2/7] Skipped (resume) — not re-running terraform apply.")

        # Shared setup needed by every phase from [3/7] on. Runs even when phase 3 itself is
        # skipped, because phases 4–7 all reference key/ctx/scoring_ip/scoring_user.
        ctx = read_terraform_ctx()
        key = Path(ctx["ssh_key_path"])
        scoring_user = os.environ["TF_VAR_vm_username"]
        scoring_ip = ctx["scoring_engine_ip"]

        # [3/7] (was: copy SSH key to scoring engine — removed, nothing ever read the copy back;
        # all SSH/SCP to team/scoring boxes uses the local key via ctx, and nakon authenticates
        # to team boxes by password.)
        if from_phase <= 3:
            current_phase = 3
            print("[3/7] Skipped — remote key copy removed (was unused).")
            checkpoint(3)
        else:
            print("[3/7] Skipped (resume).")

        # [4/7] Bootstrap scoring engine
        if from_phase <= 4:
            current_phase = 4
            print("[4/7] Bootstrapping scoring engine (packages, Docker, Quotient)...")
            bootstrap_scoring_engine(ctx, postgres_password, redis_password)

            # Push event.conf now, BEFORE nakon. Quotient's server panics on every scoring round
            # while event.conf is absent, and each container restart re-syncs iptables and wipes
            # the team NAT rule — which starves nakon's apt-get of internet. Writing a valid
            # event.conf here stops the crash loop so NAT stays up through the nakon runs.
            # event.conf uses the 192.168._.N wildcard and lists every team, so it's complete even
            # though team2+ boxes don't exist yet.
            print("  Pushing event.conf early (stabilizes Quotient so NAT survives nakon)...")
            push_event_conf(comp_dir, teams, boxes, ctx, name,
                            inject_password=inject_password, admin_password=admin_password,
                            postgres_password=postgres_password, redis_password=redis_password,
                            box_creds=box_creds)
            ensure_nat_forwarding(ctx)

            # [4.5/7] Enable password auth + NOPASSWD sudo for ubuntu on team1 boxes
            print("[4.5/7] Enabling password auth + NOPASSWD sudo for ubuntu on team1 boxes...")
            # Only team1 exists at this point (team2+ are cloned later)
            team1_only = {k: v for k, v in teams.items() if k == "team1"}
            setup_ubuntu_auth(team1_only, boxes, ctx)
            checkpoint(4)
        else:
            print("[4/7] Skipped (resume).")

        # [5/7] Fix DNS on team1 boxes (needed for apt-get in Nakon) + run Nakon deployment
        if from_phase <= 5:
            current_phase = 5
            print("[5/7] Fixing DNS on team1 boxes, then running Nakon deployment...")
            # Only team1 exists at this point — fix DNS so apt-get can resolve repos
            team1_only = {k: v for k, v in teams.items() if k == "team1"}
            fix_dns_on_boxes(team1_only, boxes, ctx)

            # Narrow the deploy to team1 (team2+ don't exist yet). This only decides which
            # machines get connected to — what gets deployed to each one is fixed by the
            # bundle, which was built from the full machine list before Terraform ran.
            #
            # This used to rewrite the machine list to team1, deploy, then write the full list
            # back. If anything failed in between — or the run was interrupted — the file was
            # left holding one team, and a --from-phase resume then silently deployed to one
            # team. `nakon deploy --only` expresses the same thing without mutating anything.
            team1_identifier = teams["team1"]["identifier"]
            team1_machines = [
                m["name"] for m in json.loads(nakon_config_path.read_text())["machines"]
                if m["ip"].split(".")[2] == str(team1_identifier)
            ]

            # Make sure the boxes can still reach the internet right before nakon's apt-get runs.
            ensure_nat_forwarding(ctx)

            # nakon deploys machines sequentially (confirmed via deploy logs — one machine's
            # plan finishes before the next starts), so the SSH timeout has to scale with how
            # many machines and configs this run is actually driving, not stay a flat constant.
            # PER_MACHINE_NAKON_BUDGET (2400s) is sized off practice-fix-templates' heaviest box
            # (~40 configs, several real package installs) with headroom — a smaller Compfile
            # finishes well under it, a bigger one scales the timeout instead of hitting a wall
            # mid-deploy like a flat 2400s did once config sets grew past the original ~4/box.
            run_nakon(key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
                      only=team1_machines,
                      timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(team1_machines)))
            print("  Nakon deployment complete")
            checkpoint(5)
        else:
            print("[5/7] Skipped (resume).")

        # [6/7] Clone team1 boxes to other teams, fix DNS, harden services
        if from_phase <= 6:
            current_phase = 6
            print("[6/7] Cloning team1 boxes to other teams, fixing DNS, hardening services...")
            clone_team_boxes(teams, boxes, ctx, comp_dir, box_creds=box_creds)

            # Deploy Nakon on team2+ boxes (team1 already done at [5/7])
            if len(teams) > 1:
                print("  Deploying Nakon on team2+ boxes...")
                ensure_nat_forwarding(ctx)
                # Same bundle as phase 5, with no --only, so this run reaches every team. The
                # clones already carry team1's applied configurations; re-running is deliberate
                # and matches the previous behaviour. Scale the timeout by the FULL machine
                # count (every team, not just team1) — see the phase-5 call for why this can't
                # stay a flat constant.
                all_machines = json.loads(nakon_config_path.read_text())["machines"]
                run_nakon(key, scoring_user, scoring_ip, nakon_bundle, nakon_config_path,
                          timeout=max(2400, PER_MACHINE_NAKON_BUDGET * len(all_machines)))
                print("  Nakon deployment on team2+ complete")
            else:
                # Single team: clone_team_boxes() returns early without hardening services or
                # creating the credlist OS accounts (admin/user1/user2), so auth checks would
                # score down. Run that step here for the one-team case.
                print("  Single team — hardening services on team1 boxes...")
                fix_services_on_boxes(comp_dir, teams, boxes, ctx, box_creds=box_creds)
            checkpoint(6)
        else:
            print("[6/7] Skipped (resume).")

        # [7/7] Seed competition and create injects (event.conf was pushed at [4/7])
        if from_phase <= 7:
            current_phase = 7
            print("[7/7] Seeding competition and creating injects...")

            # Poll Quotient's HTTP endpoint instead of a blind sleep before seeding.
            wait_for_http(f"http://{scoring_ip}/api/login", timeout=120)

            quotient_ctx = {
                "teams": {team_key: team_data["identifier"] for team_key, team_data in teams.items()},
                "quotient_admin_password": admin_password,
            }

            # Each sub-step is gated on its own state flag (not just phase 7's checkpoint) so a
            # resume that re-enters phase 7 — e.g. a crash between seeding and injects — can't
            # re-run a step that already succeeded. This matters most for unpause_engine(): its
            # /api/engine/pause call decrements a server-side WaitGroup exactly once (see its
            # docstring in quotient/setup.py) and isn't safe to call twice.
            if not state.get("seeded"):
                print("  Seeding teams and starting the competition clock...")
                seed_teams(scoring_ip, quotient_ctx)
                state["seeded"] = True
                _save_state()
            else:
                print("  Teams already seeded (resume) — skipping.")

            if not state.get("engine_unpaused"):
                unpause_engine(scoring_ip, quotient_ctx)
                state["engine_unpaused"] = True
                _save_state()
            else:
                print("  Engine already unpaused (resume) — skipping.")

            # Create injects via Quotient's API (competition must be seeded/started first).
            # create_injects() itself also dedupes by title (defense in depth), but the flag
            # here avoids even querying/re-posting on an otherwise-clean resume.
            if injects and not state.get("injects_created"):
                print(f"  Creating {len(injects)} inject(s)...")
                create_injects(scoring_ip, admin_password, injects)
                state["injects_created"] = True
                _save_state()
            elif injects:
                print("  Injects already created (resume) — skipping.")
            checkpoint(7)
        else:
            print("[7/7] Skipped (resume).")
    except BaseException as e:
        print(f"\n  [!] Deploy failed during phase {current_phase} of '{comp_name}'.")
        resume_phase = current_phase
        # A failure that smells like a Proxmox/Terraform state mismatch (an orphaned VM/config
        # from a prior interrupted apply, most often) can't be fixed by resuming from the same
        # phase — phase 2+ resumes explicitly skip phase 1's cleanup ("[1/7] Skipped (resume) —
        # leaving existing VMs/bridges in place."), so retrying the exact suggested command just
        # reproduces the identical error. Point at phase 1 instead when we see that signature.
        if current_phase >= 2 and "already exists" in str(e).lower():
            resume_phase = 1
            print("      This looks like a Proxmox/Terraform state mismatch (something the "
                  "prior attempt created still exists, but Terraform's state doesn't know about "
                  "it) — resuming from the failed phase would just hit the same error again.")
        print(f"      Resume with: python3 create-competition.py "
              f"--competition {comp_name} --from-phase {resume_phase} --yes")
        raise

    # Persist all credentials to a mode-0600 file so operators have a durable, non-log
    # record (the summary below still prints them for convenience, but the file is the
    # authoritative copy and is chmod 600 so it isn't world-readable).
    cred_lines = [
        f"# Credentials for {name} — generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scoreboard:  http://{scoring_ip}",
        f"admin  {admin_password}",
    ]
    if inject_password:
        cred_lines.append(f"inject  {inject_password}")
    for team_name, team_data in teams.items():
        cred_lines.append(f"{team_name}  {team_data['password']}  (192.168.{team_data['identifier']}.0/24)")
    # Box login (the 'ubuntu' cloud-init account) and the credlist accounts Quotient's
    # Ssh/Smtp/Imap/Sql/Ftp checks authenticate WITH — generated fresh per competition (see
    # box_password/box_creds above), recorded here since this file is the one durable,
    # non-log record of every secret this run generated.
    cred_lines.append(f"box-login (ubuntu)  {box_password}")
    for user, pw in box_creds.items():
        cred_lines.append(f"box-credlist-{user}  {pw}")
    cred_path = comp_dir / "credentials.txt"
    cred_path.write_text("\n".join(cred_lines) + "\n")
    os.chmod(cred_path, 0o600)

    # Print summary
    print(f"\n{'='*60}")
    print(f"  {name} is live")
    print(f"{'='*60}")
    print(f"Scenario: {scenario}")
    print(f"Saved to: competitions/{comp_name}/  (credentials.txt, mode 0600)")
    print(f"\nScoreboard:    http://{scoring_ip}")
    print(f"Admin login:   admin / {admin_password}")
    if inject_password:
        print(f"Inject login:  inject / {inject_password}   ({len(injects)} inject(s) loaded)")
    print(f"\nTeam logins:")
    for team_name, team_data in teams.items():
        print(f"  {team_name} / {team_data['password']}  (subnet 192.168.{team_data['identifier']}.0/24)")
    print(f"\nBox login:     ubuntu / {box_password}  (every team box)")
    print(f"Box credlist:  " + ", ".join(f"{u}/{p}" for u, p in box_creds.items()))
    print(f"\nScoring engine SSH: ssh -i {key} {scoring_user}@{scoring_ip}")
    print(f"{'='*60}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Tezcatlipoca — CTF Range Deployment. With no flags it runs fully "
                    "interactively (as before); flags let it run non-interactively.",
    )
    parser.add_argument("--competition", help="Competition name. If it already has a Compfile + "
                                              "boxes.json, deploy it straight away; otherwise it "
                                              "is created (needs --scenario/--difficulty or falls "
                                              "back to prompting).")
    parser.add_argument("--teams", type=int, help="Number of teams (skips the 'How many teams?' prompt).")
    parser.add_argument("--yes", action="store_true", help="Skip the confirm-deploy prompt.")
    parser.add_argument("--scenario", help="Scenario description (only when creating a new competition).")
    parser.add_argument("--difficulty", type=int, help="Difficulty 1-10 (only when creating a new competition).")
    parser.add_argument("--from-phase", type=int, default=1, dest="from_phase",
                        help="Resume from this phase (>1 skips the destructive cleanup + terraform "
                             "apply). See the resume hint printed on a failed deploy.")
    parser.add_argument("--plan-only", action="store_true", dest="plan_only",
                        help="Collect/generate the competition's config (Compfile, boxes.json) "
                             "and print a summary, then exit WITHOUT touching any infrastructure "
                             "— no teardown, no `terraform apply`. There is no confirmation "
                             "checkpoint between the box picker and a real deploy otherwise "
                             "(--yes skips it outright, and piped/scripted stdin that happens to "
                             "satisfy every remaining prompt walks straight into one) — use this "
                             "to review the plan first, then re-run without the flag to deploy it "
                             "for real.")
    args = parser.parse_args()

    def _print_plan(comp_name, comp_dir):
        boxes = json.loads((comp_dir / "boxes.json").read_text())
        print(f"\n  ── PLAN for '{comp_name}' — nothing has been deployed " + "─" * 20)
        for b in boxes:
            disk = f"{b['disk_gb']} GB disk" if b.get("disk_gb") else "template's own disk"
            print(f"    {b['name']:<12} {b['template']:<20} {b['cpu']} CPU, {b['memory_mb']} MB, {disk}")
        print(f"\n  Team count is decided at deploy time (--teams N, or the prompt).")
        print(f"  Nothing was deployed — no teardown, no terraform apply.")
        print(f"  Deploy for real with: python3 create-competition.py --competition {comp_name} "
              f"--teams <N> --yes")

    print("Tezcatlipoca - CTF Range Deployment")
    print("=" * 40)

    # Non-interactive path: --competition names the competition to deploy or create.
    if args.competition:
        comp_name = args.competition.strip().lower().replace(" ", "-")
        comp_dir = Path("competitions") / comp_name
        has_compfile = (comp_dir / "Compfile").exists()
        has_boxes = (comp_dir / "boxes.json").exists()

        if comp_dir.is_dir() and has_compfile and has_boxes:
            # Reuse existing competition — straight to deploy(), no stdin needed.
            print(f"Reusing existing competition '{comp_name}'.")
        else:
            # Create the competition. scenario/difficulty come from flags when given, otherwise
            # we fall back to prompting for just the missing pieces. boxes are still collected
            # interactively (there's no non-interactive box spec yet).
            print(f"Creating new competition '{comp_name}'.")
            comp_dir.mkdir(parents=True, exist_ok=True)
            scenario = args.scenario if args.scenario is not None else input("Scenario description: ").strip()
            difficulty = args.difficulty if args.difficulty is not None else int(input("Difficulty (1-10): "))
            (comp_dir / "Compfile").write_text(
                f"name {comp_name}\n"
                f"scenario {scenario}\n"
                f"difficulty {difficulty}\n"
            )
            boxes = collect_boxes()
            (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))

        if args.plan_only:
            _print_plan(comp_name, comp_dir)
            return

        deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase)
        return

    # Interactive path (unchanged behavior when no --competition flag is given).
    previous = load_previous_competitions()
    if previous:
        print("\nPrevious competitions:")
        for i, name in enumerate(previous, 1):
            print(f"  [{i}] {name}")
        print()

    choice = input("Create new (n) or reuse existing (number)? ").strip()

    if choice.lower() == "n":
        comp_name = input("Competition name: ").strip().lower().replace(" ", "-")
        comp_dir = Path("competitions") / comp_name
        comp_dir.mkdir(parents=True, exist_ok=True)

        scenario = input("Scenario description: ").strip()
        difficulty = int(input("Difficulty (1-10): "))

        # Write Compfile in key=value format (matching utils.load_compfile)
        (comp_dir / "Compfile").write_text(
            f"name {comp_name}\n"
            f"scenario {scenario}\n"
            f"difficulty {difficulty}\n"
        )

        boxes = collect_boxes()
        (comp_dir / "boxes.json").write_text(json.dumps(boxes, indent=2))
    else:
        idx = int(choice) - 1
        if 0 <= idx < len(previous):
            comp_name = previous[idx]
        else:
            print("Invalid choice.")
            sys.exit(1)
        comp_dir = Path("competitions") / comp_name

    if args.plan_only:
        _print_plan(comp_name, comp_dir)
        return

    # Pass through --teams/--yes/--from-phase so they still work in interactive mode; they're
    # None/False/1 by default, giving exactly the prior interactive behavior.
    deploy(comp_dir, num_teams=args.teams, assume_yes=args.yes, from_phase=args.from_phase)


if __name__ == "__main__":
    main()
