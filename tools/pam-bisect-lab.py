#!/usr/bin/env python3
"""PAM/SSH bisect lab for the planted-box restart trap (docs/known-issues.md).

The bisect config set is JSON:

  [{"name": "pam-auth-faillock", "omit": "rm -f /etc/pam.d/common-auth"},
   {"name": "security-limits",   "omit": "rm -f /etc/security/limits.conf"}]

`omit` runs as root through the guest agent against the rolled-back failing snapshot.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import urllib3
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

ENV_PATH = REPO / ".env"
if not os.environ.get("TEZ_TEST_OFFLINE"):
    load_dotenv(ENV_PATH)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from common.pam_bundle import capture_pam_bundle  # noqa: E402
from common.ssh_ops import classify_ssh_failure  # noqa: E402
from common.utils import mint_run_id  # noqa: E402
from estate import config  # noqa: E402
from infra.client import proxmox_api, wait_for_proxmox_task  # noqa: E402
from infra.guest_exec import guest_agent_exec_root, wait_for_guest_agent  # noqa: E402
from infra.lifecycle import rollback_snapshot, start_vm, stop_vm  # noqa: E402
from infra.ownership import Ownership, clone_marker, destroy_vm_if_exists  # noqa: E402

LAB_COMP = "pam-lab"
LAB_TAG = "pam-lab"


def _load_state(path):
    p = Path(path)
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"  ERROR: cannot read lab state {p}: {e}")
    for key in ("node", "vmid", "tags"):
        if key not in doc:
            raise SystemExit(f"  ERROR: lab state {p} has no '{key}' — re-run `up`")
    return doc


def _write_state(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def _template_vmid(node, template):
    if template.isdigit():
        return int(template)
    vms = proxmox_api("GET", f"/nodes/{node}/qemu")["data"]
    matches = [int(v["vmid"]) for v in vms if v.get("name") == template]
    if len(matches) != 1:
        raise SystemExit(f"  ERROR: template '{template}' matches {len(matches)} VM(s) on "
                         f"'{node}' — name one template, or pass its vmid")
    return matches[0]


def _guest_ipv4(node, vmid):
    try:
        ifaces = proxmox_api(
            "GET", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
        )["data"]["result"]
    except Exception as e:                           # noqa: BLE001 - report a usable remedy
        raise SystemExit(f"  ERROR: guest agent could not report an address for vmid {vmid} "
                         f"({type(e).__name__}) — pass --ip")
    for iface in ifaces:
        for addr in iface.get("ip-addresses") or []:
            ip = addr.get("ip-address") or ""
            if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                return ip
    raise SystemExit(f"  ERROR: vmid {vmid} reports no IPv4 address — pass --ip")


def _engine_ctx(args):
    cfg = config.active()
    engine_ip = args.engine_ip or cfg.engine.mgmt_ip
    if not engine_ip:
        raise SystemExit("  ERROR: no engine management IP in the profile — pass --engine-ip "
                         "(box SSH is tunnelled through the scoring engine)")
    # Default TOFU pins the pre-reboot engine key, faking "unreachable" after engine reboots.
    lab = getattr(args, "lab", None)
    return {"ssh_key_path": str(config.ssh_key_path()),
            "scoring_engine_ip": engine_ip,
            "known_hosts": str(Path(lab).parent / "known_hosts") if lab else None,
            "vm_username": args.engine_user or cfg.engine.username,
            "box_username": args.user}


def load_configs(path):
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"  ERROR: cannot read config set {path}: {e}")
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"  ERROR: {path} must hold a non-empty JSON list")
    configs = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not item.get("name") or not item.get("omit"):
            raise SystemExit(f"  ERROR: {path}[{i}] must be "
                             f"{{\"name\": \"...\", \"omit\": \"shell command\"}}")
        configs.append({"name": str(item["name"]), "omit": str(item["omit"])})
    return configs


def correlate(rows):
    return [row["name"] for row in rows if row["verdict"] == "healthy"]


def run_bisect(configs, retest, echo=print):
    baseline = retest(None)
    echo(f"  baseline (no omission): {baseline}")
    if baseline != "pam-trap":
        echo("  the failing snapshot did not reproduce pam-trap at baseline — no correlation "
             "can be claimed")
        return {"baseline": baseline, "rows": [], "correlates": []}
    rows = []
    for cfg in configs:
        verdict = retest(cfg)
        rows.append({"name": cfg["name"], "verdict": verdict})
        echo(f"  omit {cfg['name']}: {verdict}")
    hits = correlate(rows)
    if hits:
        echo("  correlates: " + ", ".join(hits))
    else:
        echo("  no single config correlates — the trap persisted, or the retest was "
             "inconclusive, with every config omitted")
    return {"baseline": baseline, "rows": rows, "correlates": hits}


def _restart_and_probe(node, vmid, ip, ctx, *, cycles, settle, user, agent_timeout):
    seen = []
    for _ in range(max(1, cycles)):
        stop_vm(node, vmid)
        start_vm(node, vmid)
        wait_for_guest_agent(node, vmid, timeout=agent_timeout)
        if settle:
            time.sleep(settle)
        seen.append(classify_ssh_failure(ctx, ip, user=user))
    if "pam-trap" in seen:
        return "pam-trap"
    return "healthy" if all(s == "ok" for s in seen) else seen[-1]


def cmd_grab(args):
    metadata = {"source": "pam-bisect-lab grab"}
    if args.label:
        metadata["label"] = args.label
    result = capture_pam_bundle(args.node, args.vmid, args.out, metadata=metadata)
    print(f"  {result['captured']}/{len(result['entries'])} section(s) captured into "
          f"{result['dir']}")
    return 0 if result["captured"] else 1


def cmd_up(args):
    node = args.node or config.active().proxmox.node
    src = _template_vmid(node, args.template)
    run_id = mint_run_id()
    ownership = Ownership(LAB_COMP, run_id, frozenset({LAB_TAG}))
    vmid = args.vmid
    if vmid:
        live = {int(v["vmid"]) for v in proxmox_api("GET", f"/nodes/{node}/qemu")["data"]}
        if vmid in live:
            raise SystemExit(f"  ERROR: vmid {vmid} is already in use on '{node}'")
    else:
        vmid = int(proxmox_api("GET", "/cluster/nextid")["data"])
    name = args.name or f"pamlab-{run_id.removeprefix('run-')}"
    launcher = str(Path(args.out) / "lab.json")
    doc = {"schema": 1, "lab": run_id, "node": node, "vmid": vmid, "name": name,
           "template": src, "tags": sorted(ownership.tags),
           "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    print(f"  cloning template vmid {src} -> lab vmid {vmid} ({name}) on '{node}'")
    upid = proxmox_api("POST", f"/nodes/{node}/qemu/{src}/clone", data={
        "newid": vmid, "name": name, "full": 0,
        # /clone takes no tags; the description marks a clone killed before the tagging PUT.
        "description": clone_marker(LAB_COMP),
    })["data"]
    # State written before the clone task finishes, so a failed clone still has a down path.
    _write_state(launcher, doc)
    wait_for_proxmox_task(node, upid)
    proxmox_api("PUT", f"/nodes/{node}/qemu/{vmid}/config",
                data={"tags": ";".join(sorted(ownership.tags))})
    start_vm(node, vmid)
    if wait_for_guest_agent(node, vmid, timeout=args.agent_timeout):
        print("  guest agent answers")
    print(f"  tags      : {sorted(ownership.tags)}")
    print(f"  teardown  : tools/pam-bisect-lab.py down --lab {launcher}")
    return 0


def cmd_cycle(args):
    lab = _load_state(args.lab)
    node, vmid = lab["node"], int(lab["vmid"])
    ip = args.ip or _guest_ipv4(node, vmid)
    ctx = _engine_ctx(args)
    print(f"  cycling vmid {vmid} on '{node}' ({ip}) {args.cycles} time(s)")
    traps = 0
    for i in range(1, args.cycles + 1):
        verdict = _restart_and_probe(
            node, vmid, ip, ctx, cycles=1, settle=args.settle, user=args.user,
            agent_timeout=args.agent_timeout)
        print(f"    cycle {i}/{args.cycles}: {verdict}")
        if verdict != "pam-trap":
            continue
        traps += 1
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dest = Path(args.lab).resolve().parent / f"pam-trap-{stamp}-cycle{i}"
        try:
            result = capture_pam_bundle(
                node, vmid, dest, timeout=args.capture_timeout,
                metadata={"source": "pam-bisect-lab cycle", "cycle": i, "ip": ip})
        except Exception as e:                       # noqa: BLE001 - keep cycling
            print(f"    WARNING: support-bundle capture failed ({type(e).__name__}) — "
                  f"continuing")
        else:
            print(f"    support bundle: {dest} ({result['captured']} section(s))")
    print(f"  pam-trap on {traps}/{args.cycles} cycle(s)")
    return 1 if traps else 0


def cmd_bisect(args):
    configs = load_configs(args.configs)
    lab = _load_state(args.lab)
    node, vmid = lab["node"], int(lab["vmid"])
    ip = args.ip or _guest_ipv4(node, vmid)
    ctx = _engine_ctx(args)

    def retest(cfg):
        try:
            rollback_snapshot(node, vmid, args.snapshot)
        except Exception as e:                       # noqa: BLE001 - a verdict, not a crash
            print(f"    rollback to '{args.snapshot}' failed ({type(e).__name__})")
            return "rollback-failed"
        if cfg is not None:
            try:
                rc, _out, _err = guest_agent_exec_root(
                    node, vmid, cfg["omit"], timeout=args.omit_timeout)
            except Exception as e:                   # noqa: BLE001 - a verdict, not a crash
                print(f"    omission for {cfg['name']} failed ({type(e).__name__})")
                return "omit-failed"
            if rc != 0:
                print(f"    omission for {cfg['name']} exited {rc}")
                return "omit-failed"
        return _restart_and_probe(
            node, vmid, ip, ctx, cycles=args.cycles, settle=args.settle, user=args.user,
            agent_timeout=args.agent_timeout)

    print(f"  bisecting {len(configs)} config(s) against snapshot '{args.snapshot}' "
          f"on vmid {vmid}")
    result = run_bisect(configs, retest)
    if result["baseline"] != "pam-trap":
        return 2
    return 0 if result["correlates"] else 1


def cmd_down(args):
    lab = _load_state(args.lab)
    tags = set(lab["tags"])
    if not {"tezcatlipoca", f"comp-{LAB_COMP}", LAB_TAG} <= tags:
        raise SystemExit(f"  ERROR: lab state {args.lab} lacks the pam-lab ownership tags — "
                         f"refusing to destroy anything")
    destroy_vm_if_exists(lab["node"], int(lab["vmid"]), expect_tags=tags)
    Path(args.lab).unlink()
    print(f"  destroyed lab vmid {lab['vmid']} on '{lab['node']}'; removed {args.lab}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    grab = sub.add_parser("grab", help="collect a support bundle over the guest agent")
    grab.add_argument("--node", required=True)
    grab.add_argument("--vmid", type=int, required=True)
    grab.add_argument("--out", required=True)
    grab.add_argument("--label", help="optional note stored in the bundle manifest")
    grab.set_defaults(func=cmd_grab)

    up = sub.add_parser("up", help="clone a named template into a tagged lab VM")
    up.add_argument("--template", required=True, help="template name or vmid")
    up.add_argument("--out", required=True, help="lab directory (writes lab.json here)")
    up.add_argument("--node")
    up.add_argument("--vmid", type=int)
    up.add_argument("--name")
    up.add_argument("--agent-timeout", type=int, default=300)
    up.set_defaults(func=cmd_up)

    cycle = sub.add_parser("cycle", help="stop/start N times, probe SSH, grab on pam-trap")
    cycle.add_argument("--lab", required=True)
    cycle.add_argument("--cycles", type=int, default=1)
    cycle.add_argument("--ip")
    cycle.add_argument("--user", default="ubuntu")
    cycle.add_argument("--engine-ip")
    cycle.add_argument("--engine-user")
    cycle.add_argument("--settle", type=int, default=20)
    cycle.add_argument("--agent-timeout", type=int, default=300)
    cycle.add_argument("--capture-timeout", type=int, default=30)
    cycle.set_defaults(func=cmd_cycle)

    bisect = sub.add_parser("bisect", help="omit configs one at a time against a failing snapshot")
    bisect.add_argument("--lab", required=True)
    bisect.add_argument("--snapshot", required=True)
    bisect.add_argument("--configs", required=True, help="JSON list of {name, omit}")
    bisect.add_argument("--cycles", type=int, default=1)
    bisect.add_argument("--ip")
    bisect.add_argument("--user", default="ubuntu")
    bisect.add_argument("--engine-ip")
    bisect.add_argument("--engine-user")
    bisect.add_argument("--settle", type=int, default=20)
    bisect.add_argument("--agent-timeout", type=int, default=300)
    bisect.add_argument("--omit-timeout", type=int, default=60)
    bisect.set_defaults(func=cmd_bisect)

    down = sub.add_parser("down", help="destroy exactly the lab VM recorded in lab.json")
    down.add_argument("--lab", required=True)
    down.set_defaults(func=cmd_down)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
