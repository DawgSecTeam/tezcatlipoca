"""Cross-node template sync: copy a VM template from one nodes.json node to another
over root SSH (`vzdump --stdout | ssh qmrestore`).

Satellites need their own copies of every box template (plus an alpine base for the
jump VM clone) before they can host teams — linked clones and full clones never
cross hosts on separate storages. This is the tool that closes that gap; the
per-node template gates (preflight_gates_multinode, placement probes) point here on
failure."""

import os
import subprocess

from nodes_ops import load_nodes_config
from range_ops import proxmox_api_for

SYNC_MARKER_TAG = "template"


def resolve_record(name, records):
    for r in records:
        if r.name == name:
            return r
    raise SystemExit(f"  ERROR: no node named '{name}' in nodes.json "
                     f"(have: {', '.join(r.name for r in records)})")


def find_template_vmid(rec, template_name):
    """Template by exact name on this node, stopped + tagged `template`."""
    vms = proxmox_api_for(rec.endpoint, os.environ[rec.token_env], "GET",
                          "/cluster/resources", params={"type": "vm"})["data"]
    for vm in vms:
        if vm.get("node") != rec.node or vm.get("name") != template_name:
            continue
        tags = {t.strip() for t in str(vm.get("tags") or "").replace(";", ",").split(",") if t.strip()}
        if vm.get("template") == 1 and SYNC_MARKER_TAG in tags:
            return int(vm["vmid"])
    raise SystemExit(f"  ERROR: no stopped template named '{template_name}' on node "
                     f"'{rec.name}' ({rec.node}) — nothing to sync")


def pick_destination_vmid(rec):
    """The destination's next free VMID (PVE's own allocator)."""
    data = proxmox_api_for(rec.endpoint, os.environ[rec.token_env], "GET",
                           "/cluster/nextid")["data"]
    return int(data)


def check_root_ssh(rec):
    """Root SSH must work key-only before any stream starts."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        f"root@{rec.host()}", "echo ok"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or "ok" not in r.stdout:
        raise SystemExit(
            f"  ERROR: root SSH to '{rec.name}' ({rec.host()}) is unavailable "
            f"({(r.stderr or r.stdout).strip()[:160]}). Template sync pipes the raw "
            f"vzdump stream through root on both hosts — set up the key first.")


def plan_sync(src_rec, dst_rec, template_name):
    """The exact command list (pure — unit-tested). vzdump streams zstd to stdout;
    qmrestore consumes stdin onto the destination datastore. The restore lands as a
    plain VM at a fresh vmid; finish_sync converts + tags it via the API."""
    vmid = find_template_vmid(src_rec, template_name)
    newid = pick_destination_vmid(dst_rec)
    dump = f"vzdump {vmid} --stdout --compress zstd"
    restore = f"qmrestore - {newid} --storage {dst_rec.datastore}"
    return {
        "src": src_rec.name, "dst": dst_rec.name,
        "src_vmid": vmid, "dst_vmid": newid,
        "commands": [
            ["ssh", f"root@{src_rec.host()}", dump],
            ["ssh", f"root@{dst_rec.host()}", restore],
        ],
        "pipeline": f"ssh root@{src_rec.host()} \"{dump}\" | ssh root@{dst_rec.host()} \"{restore}\"",
    }


def run_sync(plan):
    """Execute the pipe. Raise on either side's failure with the stream's stderr."""
    p1 = subprocess.Popen(plan["commands"][0], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    p2 = subprocess.Popen(plan["commands"][1], stdin=p1.stdout,
                          stderr=subprocess.PIPE)
    p1.stdout.close()
    _, err2 = p2.communicate()
    p1.wait()
    if p2.returncode != 0 or p1.returncode != 0:
        e1 = (p1.stderr.read() or b"").decode(errors="replace").strip()
        raise SystemExit(
            f"  ERROR: template sync failed (vzdump rc={p1.returncode}, qmrestore "
            f"rc={p2.returncode}): {(err2 or b'').decode(errors='replace').strip()[:300]}"
            f"{(' | src: ' + e1[:200]) if e1 else ''}")
    return plan["dst_vmid"]


def finish_sync(dst_rec, newid, template_name):
    """Restore lands as a plain VM: convert to template, stamp the `template` tag,
    make sure it is stopped."""
    token = os.environ[dst_rec.token_env]
    cfg = proxmox_api_for(dst_rec.endpoint, token, "GET",
                          f"/nodes/{dst_rec.node}/qemu/{newid}/config")["data"]
    if not cfg.get("template"):
        proxmox_api_for(dst_rec.endpoint, token, "POST",
                        f"/nodes/{dst_rec.node}/qemu/{newid}/template")
    tags = {t.strip() for t in str(cfg.get("tags") or "").replace(";", ",").split(",") if t.strip()}
    tags.add(SYNC_MARKER_TAG)
    proxmox_api_for(dst_rec.endpoint, token, "PUT",
                    f"/nodes/{dst_rec.node}/qemu/{newid}/config",
                    data={"tags": ";".join(sorted(tags))})
    print(f"  '{template_name}' is now a template on '{dst_rec.name}' (vmid {newid})")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Copy a VM template between nodes.json nodes (vzdump | qmrestore "
                    "over root SSH). Satellites need their own template copies plus an "
                    "alpine base (the jump clone source) before they can host teams.")
    parser.add_argument("--template", required=True, help="exact template VM name")
    parser.add_argument("--from", dest="src", required=True, help="source nodes.json node name")
    parser.add_argument("--to", dest="dst", required=True, help="destination nodes.json node name")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the planned commands and resolved vmids, change nothing")
    args = parser.parse_args()

    records, _ = load_nodes_config()
    if records is None:
        raise SystemExit("  ERROR: no nodes.json — template sync is a multi-node tool")
    src_rec = resolve_record(args.src, records)
    dst_rec = resolve_record(args.dst, records)
    if src_rec.name == dst_rec.name:
        raise SystemExit("  ERROR: --from and --to are the same node")
    for rec in (src_rec, dst_rec):
        if not os.environ.get(rec.token_env):
            raise SystemExit(f"  ERROR: token env '{rec.token_env}' for node "
                             f"'{rec.name}' is empty — add it to .env")

    check_root_ssh(src_rec)
    check_root_ssh(dst_rec)
    plan = plan_sync(src_rec, dst_rec, args.template)
    print(f"  Sync '{args.template}': {plan['src']} (vmid {plan['src_vmid']}) -> "
          f"{plan['dst']} (vmid {plan['dst_vmid']}, storage {dst_rec.datastore})")
    if args.dry_run:
        print(f"  {plan['pipeline']}")
        print("  --dry-run: nothing was changed.")
        return
    newid = run_sync(plan)
    finish_sync(dst_rec, newid, args.template)


if __name__ == "__main__":
    main()
