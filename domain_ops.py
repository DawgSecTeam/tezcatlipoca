"""AD forest promotion and domain-join orchestration."""

import json
import os

from constants import WINDOWS_ADMIN_USER
from nakon_ops import _run_single_nakon_config
from range_ops import (
    box_index,
    guest_agent_exec_root,
    guest_agent_exec_windows,
    vm_id_for,
    wait_for_guest_agent,
)
from windows_ops import (
    dns_repoint_windows_box,
    is_windows_template,
    wait_for_dc_dns,
    wait_for_windows_sshd,
)


def _probe_joined(node, member_box, member_vmid, domain):
    """Live membership probe via the guest agent; joins are not idempotent, so resumes skip joined members."""
    try:
        if is_windows_template(member_box["template"]):
            rc, out, _ = guest_agent_exec_windows(
                node, member_vmid,
                "(Get-WmiObject Win32_ComputerSystem).PartOfDomain; "
                "(Get-WmiObject Win32_ComputerSystem).Domain", timeout=60)
            lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
            return len(lines) >= 1 and lines[0].lower() == "true" \
                and len(lines) >= 2 and lines[1].lower() == domain.lower()
        rc, out, _ = guest_agent_exec_root(
            node, member_vmid,
            f"realm list 2>/dev/null | grep -qi 'domain-name: *{domain}' "
            f"&& echo JOINED || echo NOT", timeout=60)
        return "JOINED" in (out or "")
    except Exception as e:
        print(f"      (membership probe failed, will attempt join: {e})")
        return False


def deploy_domain_configs(teams, boxes, comp_dir, nakon_config_path, key, scoring_user,
                          scoring_ip, box_password, promote_dc=True):
    """Per-team AD forest promotion + member joins from domain_roles.json; no-op when absent."""
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        return
    roles = json.loads(roles_path.read_text())
    if not roles:
        return

    node = os.environ["TF_VAR_proxmox_node"]
    all_machines = {m["name"]: m for m in json.loads(nakon_config_path.read_text())["machines"]}
    dc_box = next((b for b in boxes if roles.get(b["name"]) == "dc"), None)
    member_boxes = [b for b in boxes if roles.get(b["name"]) == "member"]
    if dc_box is None:
        print("  WARNING: domain_roles.json has no 'dc' box — skipping domain configuration.")
        return

    for team_key, team in teams.items():
        identifier = team["identifier"]
        domain = f"team{identifier}.local"
        dc_machine_name = f"{dc_box['name']}-team{identifier}"
        dc_machine = all_machines.get(dc_machine_name)
        if dc_machine is None:
            print(f"  WARNING: {team_key}: machine {dc_machine_name} not in nakon config — "
                  f"skipping domain setup for this team")
            continue
        dc_vmid = vm_id_for(identifier, box_index(boxes, dc_box["name"]))
        dc_ip = dc_machine["ip"]

        if not promote_dc:
            print(f"  [{team_key}] DC {dc_box['name']} left as-is — (re)joining member "
                  f"box(es) to existing {domain}...")
        elif (comp_dir / f".nakon-domain-{team_key}-adds.json").exists():
            print(f"  [{team_key}] ADDS artifact present — DC {dc_box['name']} assumed "
                  f"promoted, skipping promotion (resume)")
        else:
            print(f"  [{team_key}] Promoting {dc_box['name']} ({dc_ip}) to a new AD forest "
                  f"({domain})...")
            _run_single_nakon_config(
                dc_machine,
                [{"name": "ADDS", "vars": {"domain": domain, "dsrm_password": box_password}}],
                key, scoring_user, scoring_ip, comp_dir, tag=f"{team_key}-adds",
            )
            print(f"    Waiting for {dc_box['name']} to reboot and come back (AD DS promotion "
                  f"is slow — budgeting up to 20 min)...")
            if not wait_for_guest_agent(node, dc_vmid, timeout=1200):
                print(f"  WARNING: {dc_box['name']} guest agent never came back after ADDS — "
                      f"skipping the rest of {team_key}'s domain setup")
                continue
            wait_for_windows_sshd(node, dc_vmid, timeout=180)

            print(f"  [{team_key}] Planting AD-flavored misconfigs on {dc_box['name']}...")
            _run_single_nakon_config(
                dc_machine,
                [
                    {"name": "Add User Account", "vars": {
                        "username": "svc-support", "password": box_password,
                        "full_name": "IT Support", "domain_address": domain,
                    }},
                    {"name": "Elevate User Account", "vars": {"username": "svc-support"}},
                    {"name": "Disable System Firewall"},
                    {"name": "Removing all auditing"},
                ],
                key, scoring_user, scoring_ip, comp_dir, tag=f"{team_key}-ad-misconfigs",
                strict=False,
            )

        if member_boxes:
            print(f"  [{team_key}] Waiting for {dc_box['name']}'s DNS to serve {domain}...")
            if not wait_for_dc_dns(node, dc_vmid, domain, dc_ip):
                print(f"  WARNING: {dc_box['name']} DNS never served SRV records for {domain} "
                      f"— member joins may fail")

        for member_box in member_boxes:
            member_machine_name = f"{member_box['name']}-team{identifier}"
            member_machine = all_machines.get(member_machine_name)
            if member_machine is None:
                print(f"  WARNING: {team_key}: machine {member_machine_name} not in nakon "
                      f"config — skipping")
                continue
            member_vmid = vm_id_for(identifier, box_index(boxes, member_box["name"]))
            member_ip = member_machine["ip"]

            if is_windows_template(member_box["template"]):
                if _probe_joined(node, member_box, member_vmid, domain):
                    print(f"  [{team_key}] {member_box['name']} already joined to {domain} — "
                          f"skipping join (resume)")
                    continue
                print(f"  [{team_key}] Repointing {member_box['name']}'s DNS at "
                      f"{dc_box['name']} ({dc_ip}) so it can find the domain...")
                dns_repoint_windows_box(node, member_vmid, dc_ip)

                print(f"  [{team_key}] Joining {member_box['name']} to {domain}...")
                _run_single_nakon_config(
                    member_machine,
                    [{"name": "Domain Join", "vars": {
                        "domain": domain, "admin_user": WINDOWS_ADMIN_USER,
                        "admin_pass": box_password,
                    }}],
                    key, scoring_user, scoring_ip, comp_dir,
                    tag=f"{team_key}-{member_box['name']}-join",
                )
                print(f"    Waiting for {member_box['name']} to reboot and come back "
                      f"(domain join)...")
                if not wait_for_guest_agent(node, member_vmid, timeout=900):
                    print(f"  WARNING: {member_box['name']} guest agent never came back after "
                          f"Domain Join")
                    continue
                wait_for_windows_sshd(node, member_vmid, timeout=180)
            else:
                if _probe_joined(node, member_box, member_vmid, domain):
                    print(f"  [{team_key}] Linux member {member_box['name']} already joined to "
                          f"{domain} — skipping join (resume)")
                    continue
                print(f"  [{team_key}] Joining Linux member {member_box['name']} ({member_ip}) "
                      f"to {domain} via realmd/sssd...")
                try:
                    _run_single_nakon_config(
                        member_machine,
                        [{"name": "domain-join", "vars": {
                            "DOMAIN": domain,
                            "DC_IP": dc_ip,
                            "DOMAIN_ADMIN_USER": WINDOWS_ADMIN_USER,
                            "DOMAIN_ADMIN_PASS": box_password,
                            "BOX_HOSTNAME": member_box["name"],
                        }}],
                        key, scoring_user, scoring_ip, comp_dir,
                        tag=f"{team_key}-{member_box['name']}-join",
                        strict=False,
                    )
                except Exception as e:
                    print(f"  WARNING: [{team_key}] {member_box['name']} domain-join failed "
                          f"— continuing ({str(e)[:160]})")
