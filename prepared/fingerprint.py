"""What a prepared range IS at its restore point, so a reset can prove it got that back.

A rollback guarantees the disks and nothing else. Everything that is not a disk — the engine's
DNAT/FORWARD rules, which Sliver daemon owns a transport port, whether a scored service came up
after this boot, the frozen payload matrix, the VM definitions in the hypervisor — is rebuilt by
whatever the boot happens to do, and every one of those has already cost a round. So a deploy
reads that state once, at the moment it takes `tz-ready`, and files it beside the range;
`redeploy --mode prepared` reads it again after the rollback and names every field that differs.
An empty diff is what "the same point a fresh deploy leaves" means, in writing.

The probes return raw text and the parsers below turn it into comparable values: keeping the two
apart is what makes this testable offline against the transcripts of a real range.
"""

import json
import re
from pathlib import Path

from config.config_ops import write_state
from infra.client import proxmox_api
from infra.guest_exec import guest_agent_exec_root, guest_agent_exec_windows
from infra.lifecycle import list_snapshots
from red.red01_ops import DNAT_CHAIN, FORWARD_CHAIN, SNAT_CHAIN

REFERENCE = ".prepared-fingerprint.json"

# `qm` puts a live view and a config digest in every GET; only the definition survives a
# rollback to compare against, so the runtime answer is dropped rather than normalised.
_VOLATILE_VM_KEYS = ("digest", "status", "lock", "agent", "uptime", "template", "id", "name")

_BOX_LINUX = "ss -H -ltn | awk '{print $5}' | sed 's/.*://' | sort -n -u"
_BOX_WINDOWS = ("(Get-NetTCPConnection -State Listen | ForEach-Object LocalPort "
                "| Sort-Object -Unique)")
_ENGINE_RULES = ("; ".join(f"iptables -S {c} 2>/dev/null"
                           for c in (DNAT_CHAIN, FORWARD_CHAIN, SNAT_CHAIN))
                 + "; iptables -S FORWARD 2>/dev/null | grep -F " + FORWARD_CHAIN
                 + "; iptables -t nat -S PREROUTING 2>/dev/null | grep -F " + DNAT_CHAIN
                 + "; iptables -t nat -S POSTROUTING 2>/dev/null | grep -F " + SNAT_CHAIN)
_RED_STATE = ("systemctl list-unit-files --no-legend --no-pager 'tlaloc*' 'sliver*'; "
              "echo '#active'; systemctl is-active tlaloc.service; echo '#daemons'; "
              "pgrep -af sliver-server; echo '#listeners'; sudo -n ss -H -ltunp")


def spec(comp_dir):
    """The range's inventory, read out of its own state document: both the deploy and the reset
    hold that file, so neither has to carry a context object into the probes."""
    from common.targets import frozen_roster
    from common.windows_ops import is_windows_template
    from estate import config, state
    comp_dir = Path(comp_dir)
    st = state.read_flat(comp_dir) or {}
    ident = st.get("identity") or {}
    node = ident.get("node") or ""
    windows = set()
    boxes_path = comp_dir / "boxes.json"
    if boxes_path.exists():
        windows = {b["name"] for b in json.loads(boxes_path.read_text())
                   if is_windows_template(b.get("template") or "")}
    targets = (frozen_roster(comp_dir) or {"targets": {}})["targets"]
    boxes = [(f"{t['team_key']}-{t['box_name']}", t["vmid"], t.get("node") or node, t["ip"],
              t["box_name"] in windows) for _k, t in sorted(targets.items())]
    red = st.get("red") or {}
    engine_vmid = ident.get("scoring_vm_id")
    ports = None
    if red.get("red_ip") or red.get("ip"):
        from red import sliver_ops
        if sliver_ops.sliver_enabled(comp_dir):
            ports = {"daemon_port": str(sliver_ops.DAEMON_PORT),
                     "server": sorted({p["server"] for p in
                                       sliver_ops.transport_ports().values()})}
    return {
        "engine": (engine_vmid, node),
        "boxes": boxes,
        "vms": [(b[0], b[1], b[2]) for b in boxes]
               + [("engine", engine_vmid, node),
                  ("red01", red.get("vmid"), red.get("node") or node)],
        "red": {"ip": red.get("red_ip") or red.get("ip"), "ports": ports,
                "ssh_key": str(config.ssh_key_path())} if red else None,
    }


def record(comp_dir):
    """Read the range's non-disk state and file it as this restore point's reference."""
    fp = capture(spec(comp_dir))
    _write(comp_dir, fp)
    return fp


def check(comp_dir):
    """Re-read and compare. [] means the range came back as it was left; anything else is
    field-by-field, in the form `path: at-snapshot=X after-rollback=Y`."""
    path = Path(comp_dir) / REFERENCE
    if not path.exists():
        # A range prepared before this existed has no reference to lose: record what this
        # reset produced, so the NEXT one has something to be measured against.
        _write(comp_dir, capture(spec(comp_dir)))
        return ["no reference for this range's tz-ready — recorded this reset's state as the "
                "reference; a deploy records it at the snapshot itself"]
    return compare(json.loads(path.read_text()), capture(spec(comp_dir)))


def _write(comp_dir, fp):
    # Atomic like every other per-run state document: a reset interrupted mid-write must not
    # leave a half file that the next one reads as the truth about the range.
    write_state(Path(comp_dir) / REFERENCE, fp)


def capture(s):
    return {"vms": _vms(s), "boxes": _boxes(s), "engine": _engine(s), "red01": _red01(s)}


def compare(reference, now):
    return [d[1:] if d.startswith(".") else d for d in _walk(reference, now, "")]


# --- probes -----------------------------------------------------------------------------

def _vms(s):
    out = {}
    for name, vmid, node in s["vms"]:
        if not vmid:                                              # a class this range lacks
            continue
        cfg = proxmox_api("GET", f"/nodes/{node}/qemu/{vmid}/config") or {}
        data = cfg.get("data") or {}
        out[name] = {"config": {k: v for k, v in sorted(data.items())
                               if k not in _VOLATILE_VM_KEYS},
                     "snapshots": sorted(list_snapshots(node, int(vmid)))}
    return out


def _agent_exec(node, vmid, script, windows=False):
    """The guest agent is the one channel that needs no credentials on a just-rolled-back VM,
    and a machine that cannot answer it is a finding to report, not a reason to crash the reset.
    """
    run = guest_agent_exec_windows if windows else guest_agent_exec_root
    try:
        _rc, raw, err = run(node, int(vmid), script)
    except Exception as exc:                                    # noqa: BLE001 - reported below
        return "unreachable:" + type(exc).__name__
    return (raw or "") + (err or "")


def _boxes(s):
    out = {}
    for name, vmid, node, windows in s["boxes"]:
        raw = _agent_exec(node, vmid, _BOX_WINDOWS if windows else _BOX_LINUX, windows)
        out[name] = raw if raw.startswith("unreachable:") else parse_ports(raw)
    return out


def _engine(s):
    vmid, node = s["engine"]
    raw = _agent_exec(node, vmid, _ENGINE_RULES)
    return {"rules": raw if raw.startswith("unreachable:") else parse_rules(raw)}


def _red01(s):
    if not s["red"]:
        return {}
    from red import tlaloc_ops                                    # cycle: it imports this tree
    proc = tlaloc_ops._ssh_run(s["red"]["ssh_key"], s["red"]["ip"], _RED_STATE, timeout=120)
    return parse_red_state((proc.stdout or "") + (proc.stderr or ""), s["red"]["ports"])


# --- parsers (pure) ---------------------------------------------------------------------

def parse_ports(raw):
    """Listening ports as a sorted set; the box's own service state reduced to one line."""
    ports = {int(p) for p in re.findall(r"(?m)^\s*(\d{1,5})\s*$", raw)}
    return sorted(ports)


def parse_rules(raw):
    """iptables -S output, normalised: rule order inside a chain is the install order, so it is
    kept; the parent-chain hooks are collected because an unhooked chain is invisible traffic."""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return sorted(ln for ln in lines if ln.startswith(("-A", "-N")) or "RANGE-RED" in ln)


def parse_red_state(raw, transport_ports):
    """Units, which daemon owns each transport port, and whether red is armed.

    `pgrep -af` gives the daemon's own --lport, so ownership is stated as `main` or
    `build:<lport>` — a process id changes every boot and means nothing across two reads.
    """
    blocks = {}
    current = "units"
    for line in raw.splitlines():
        marker = {"#active": "active", "#daemons": "daemons", "#listeners": "listeners"}\
            .get(line.strip())
        if marker:
            current = marker
            continue
        blocks.setdefault(current, []).append(line)
    units = {}
    for line in blocks.get("units", []):
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith(".service"):
            units[parts[0]] = parts[2]                          # name type state
    daemons = {}
    for line in blocks.get("daemons", []):
        pid = line.split(" ", 1)[0]
        lport = re.search(r"--lport\s+(\d+)", line)
        if transport_ports and pid.isdigit() and lport:
            daemons[pid] = "main" if lport.group(1) == transport_ports["daemon_port"] \
                else "build:" + lport.group(1)
    listeners = {}
    for line in blocks.get("listeners", []):
        port = re.search(r":(\d+)\s", line)
        pid = re.search(r"pid=(\d+)", line)
        if port and pid:
            listeners.setdefault(port.group(1), daemons.get(pid.group(1), "?"))
    wanted = {str(p) for p in (transport_ports or {}).get("server", [])}
    return {"units": units,
            "armed": any(ln.strip() == "active" for ln in blocks.get("active", [])),
            "listeners": {p: listeners.get(p, "absent") for p in sorted(wanted, key=int)}}


# --- diff -------------------------------------------------------------------------------

def _walk(before, after, path):
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for key in sorted(set(before) | set(after)):
            if key not in before:
                out.append(f"{path}.{key}: absent at snapshot, {after[key]!r} after rollback")
            elif key not in after:
                out.append(f"{path}.{key}: {before[key]!r} at snapshot, absent after rollback")
            else:
                out.extend(_walk(before[key], after[key], f"{path}.{key}"))
        return out
    if before != after:
        return [f"{path}: at-snapshot={before!r} after-rollback={after!r}"]
    return []
