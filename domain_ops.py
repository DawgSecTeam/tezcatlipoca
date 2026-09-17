"""AD forest promotion and domain-join orchestration."""

import json
import os

from constants import WINDOWS_ADMIN_USER
from nakon_ops import _run_single_nakon_config
from range_ops import vm_id_for, wait_for_guest_agent
from windows_ops import (
    dns_repoint_windows_box,
    is_windows_template,
    wait_for_dc_dns,
    wait_for_windows_sshd,
)


def deploy_domain_configs(teams, boxes, comp_dir, nakon_config_path, key, scoring_user,
                          scoring_ip, box_password, promote_dc=True):
    """Per-team AD forest promotion + member joins. Runs after clone to avoid DC clone
    duplication; reboot configs run isolated. Reads domain_roles.json; no-op if absent.
    promote_dc=False only rejoins members (redeploy case).
    """
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
        dc_vmid = vm_id_for(identifier, boxes.index(dc_box))
        dc_ip = dc_machine["ip"]

        if not promote_dc:
            print(f"  [{team_key}] DC {dc_box['name']} left as-is — (re)joining member "
                  f"box(es) to existing {domain}...")
        elif (comp_dir / f".nakon-domain-{team_key}-adds.json").exists():
            # Resume after a mid-phase-6 failure: the promotion artifact means ADDS
            # already ran for this team — re-running Install-ADDSForest on a live DC
            # would just fail. Joins below still run (they're the recoverable part).
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
            # strict=False: this pass is scoring flavor, not range infrastructure, and it can
            # still fail non-fatally even with nakon >= v0.1.3 (which fixed the duplicate
            # vars-less dependency step): "Disable System Firewall" sweeps every AD computer
            # over WinRM, and a Linux realmd member has no WinRM, so that step exits 1 on any
            # mixed Windows/Linux domain after landing its own misconfig. Failures print in
            # nakon's summary instead of aborting the deploy.
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

        # Wait for DC DNS SRV records before any member join.
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
            member_vmid = vm_id_for(identifier, boxes.index(member_box))
            member_ip = member_machine["ip"]

            if is_windows_template(member_box["template"]):
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
                # Linux member via realmd/sssd (no reboot, no separate DNS repoint).
                print(f"  [{team_key}] Joining Linux member {member_box['name']} ({member_ip}) "
                      f"to {domain} via realmd/sssd...")
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
                )
