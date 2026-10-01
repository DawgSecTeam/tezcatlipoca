"""AD forest promotion and domain-join orchestration."""

import json
import os
import time
from pathlib import Path

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
    wait_for_adws,
    wait_for_dc_dns,
    wait_for_windows_sshd,
)
from timing import timed
from utils import PRINT_LOCK, compfile_value, run_concurrent


JOIN_ATTEMPTS = 3
JOIN_RETRY_WAIT = 60


def team_domain(comp_dir, identifier):
    """Per-team AD domain name. Compfile knobs domain_prefix/domain_suffix (packet
    profiles: `mira-{team}.corp.sus` compiles to prefix 'mira-' + suffix '.corp.sus');
    default stays the historical team<identifier>.local."""
    prefix = compfile_value(Path(comp_dir) / "Compfile", "domain_prefix", "team")
    suffix = compfile_value(Path(comp_dir) / "Compfile", "domain_suffix", ".local")
    return f"{prefix}{identifier}{suffix}"


def _dc_promoted(node, dc_vmid, domain, marker_present):
    """Ask the DC whether it actually serves the team's domain before trusting a marker."""
    try:
        rc, out, _ = guest_agent_exec_windows(
            node, dc_vmid,
            "$cs = Get-WmiObject Win32_ComputerSystem; \"$($cs.DomainRole)|$($cs.Domain)\"",
            timeout=60)
        role, _, dom = (out or "").strip().partition("|")
        promoted = rc == 0 and role.isdigit() and int(role) >= 4 and dom.lower() == domain.lower()
        if marker_present and not promoted:
            print(f"      (stale ADDS marker for {domain} ignored — the DC is not promoted)")
        return promoted
    except Exception as e:
        print(f"      (DC promotion probe failed: {e} — trusting the local marker)")
        return marker_present


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
    # Packet-published AD accounts (domain_accounts.json): planted per team right after
    # the domain is up, before the misconfig pass. The CDE packet publishes blueteam +
    # the color accounts as Domain Admins — teams enumerate them in minute zero IR.
    packet_accounts = []
    accounts_path = comp_dir / "domain_accounts.json"
    if accounts_path.exists():
        try:
            packet_accounts = json.loads(accounts_path.read_text()).get("accounts") or []
        except (OSError, ValueError) as e:
            print(f"  WARNING: could not read domain_accounts.json ({e}) — no packet AD "
                  "accounts will be planted")

    def _deploy_team_domains(item):
        team_key, team = item
        identifier = team["identifier"]
        domain = team_domain(comp_dir, identifier)
        dc_machine_name = f"{dc_box['name']}-team{identifier}"
        dc_machine = all_machines.get(dc_machine_name)
        if dc_machine is None:
            with PRINT_LOCK:
                print(f"  WARNING: {team_key}: machine {dc_machine_name} not in nakon config — "
                      f"skipping domain setup for this team")
            return
        dc_vmid = vm_id_for(identifier, box_index(boxes, dc_box["name"]))
        dc_ip = dc_machine["ip"]

        if not promote_dc:
            with PRINT_LOCK:
                print(f"  [{team_key}] DC {dc_box['name']} left as-is — (re)joining member "
                      f"box(es) to existing {domain}...")
        else:
            if _dc_promoted(node, dc_vmid, domain,
                            (comp_dir / f".nakon-domain-{team_key}-adds.json").exists()):
                with PRINT_LOCK:
                    print(f"  [{team_key}] DC {dc_box['name']} already serves {domain} — "
                          f"skipping promotion (resume)")
            else:
                with PRINT_LOCK:
                    print(f"  [{team_key}] Promoting {dc_box['name']} ({dc_ip}) to a new AD "
                          f"forest ({domain})...")
                with timed(comp_dir, 6, "domain_adds", team_key):
                    _run_single_nakon_config(
                        dc_machine,
                        [{"name": "ADDS", "vars": {"domain": domain, "dsrm_password": box_password}}],
                        key, scoring_user, scoring_ip, comp_dir, tag=f"{team_key}-adds",
                    )
                with PRINT_LOCK:
                    print(f"    Waiting for {dc_box['name']} to reboot and come back (AD DS "
                          f"promotion is slow — budgeting up to 20 min)...")
                if not wait_for_guest_agent(node, dc_vmid, timeout=1200):
                    with PRINT_LOCK:
                        print(f"  WARNING: {dc_box['name']} guest agent never came back after "
                              f"ADDS — skipping the rest of {team_key}'s domain setup")
                    return
                wait_for_windows_sshd(node, dc_vmid, timeout=180)
                domain_sid = wait_for_adws(node, dc_vmid)
                with PRINT_LOCK:
                    if domain_sid:
                        print(f"  [{team_key}] {domain} is up (DomainSID {domain_sid})")
                    else:
                        print(f"  WARNING: [{team_key}] AD Web Services never answered on "
                              f"{dc_box['name']} — AD-flavored plants will likely fail")

            # The AD plants key on their OWN marker, not on first promotion: a resume
            # that finds the domain already up (lost marker, re-clone churn) used to
            # skip the 4 plants with the promotion — shakedown-5x4 recovery had to
            # replay them by hand via _run_single_nakon_config.
            if (comp_dir / f".nakon-domain-{team_key}-ad-misconfigs.json").exists():
                with PRINT_LOCK:
                    print(f"  [{team_key}] AD misconfigs already planted (resume marker)")
            else:
                with PRINT_LOCK:
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

            # Packet-published AD accounts (marker-gated like the misconfigs, so a resume
            # never re-adds). Each account is Add User Account + Elevate when admin.
            if packet_accounts:
                if (comp_dir / f".nakon-domain-{team_key}-ad-accounts.json").exists():
                    with PRINT_LOCK:
                        print(f"  [{team_key}] packet AD accounts already planted (resume marker)")
                else:
                    pins = []
                    for acct in packet_accounts:
                        pins.append({"name": "Add User Account", "vars": {
                            "username": acct["username"], "password": acct["password"],
                            "full_name": acct.get("full_name") or acct["username"],
                            "domain_address": domain,
                        }})
                        if acct.get("admin"):
                            pins.append({"name": "Elevate User Account",
                                         "vars": {"username": acct["username"]}})
                    with PRINT_LOCK:
                        print(f"  [{team_key}] Planting {len(packet_accounts)} packet AD "
                              f"account(s) on {dc_box['name']}...")
                    _run_single_nakon_config(
                        dc_machine, pins,
                        key, scoring_user, scoring_ip, comp_dir,
                        tag=f"{team_key}-ad-accounts", strict=False)

        if member_boxes:
            with PRINT_LOCK:
                print(f"  [{team_key}] Waiting for {dc_box['name']}'s DNS to serve {domain}...")
            if not wait_for_dc_dns(node, dc_vmid, domain, dc_ip):
                with PRINT_LOCK:
                    print(f"  WARNING: {dc_box['name']} DNS never served SRV records for {domain} "
                          f"— member joins may fail")

        for member_box in member_boxes:
            member_machine_name = f"{member_box['name']}-team{identifier}"
            member_machine = all_machines.get(member_machine_name)
            if member_machine is None:
                with PRINT_LOCK:
                    print(f"  WARNING: {team_key}: machine {member_machine_name} not in nakon "
                          f"config — skipping")
                continue
            member_vmid = vm_id_for(identifier, box_index(boxes, member_box["name"]))
            member_ip = member_machine["ip"]

            if is_windows_template(member_box["template"]):
                if _probe_joined(node, member_box, member_vmid, domain):
                    with PRINT_LOCK:
                        print(f"  [{team_key}] {member_box['name']} already joined to {domain} — "
                              f"skipping join (resume)")
                    continue
                with PRINT_LOCK:
                    print(f"  [{team_key}] Repointing {member_box['name']}'s DNS at "
                          f"{dc_box['name']} ({dc_ip}) so it can find the domain...")
                dns_repoint_windows_box(node, member_vmid, dc_ip)

                with PRINT_LOCK:
                    print(f"  [{team_key}] Joining {member_box['name']} to {domain}...")
                # A DC that just answered DNS can still refuse a join ("domain does not
                # exist or could not be contacted" — winad-testrun 2026-09-25), and a failed
                # join looked identical to a good one here. Confirm with the live probe and
                # retry.
                for attempt in range(1, JOIN_ATTEMPTS + 1):
                    try:
                        with timed(comp_dir, 6, "domain_join", f"{team_key}/{member_box['name']}"):
                            _run_single_nakon_config(
                                member_machine,
                                [{"name": "Domain Join", "vars": {
                                    "domain": domain, "admin_user": WINDOWS_ADMIN_USER,
                                    "admin_pass": box_password,
                                }}],
                                key, scoring_user, scoring_ip, comp_dir,
                                tag=f"{team_key}-{member_box['name']}-join",
                                strict=False,
                            )
                    except Exception as e:
                        with PRINT_LOCK:
                            print(f"  WARNING: [{team_key}] {member_box['name']} Domain Join failed "
                                  f"— continuing ({str(e)[:160]})")
                    with PRINT_LOCK:
                        print(f"    Waiting for {member_box['name']} to reboot and come back "
                              f"(domain join)...")
                    time.sleep(30)  # Add-Computer -Restart: let the reboot begin first
                    if not wait_for_guest_agent(node, member_vmid, timeout=900):
                        with PRINT_LOCK:
                            print(f"  WARNING: {member_box['name']} guest agent never came back after "
                                  f"Domain Join")
                        break
                    wait_for_windows_sshd(node, member_vmid, timeout=180)
                    if _probe_joined(node, member_box, member_vmid, domain):
                        break
                    if attempt < JOIN_ATTEMPTS:
                        with PRINT_LOCK:
                            print(f"  [{team_key}] {member_box['name']} not joined to {domain} "
                                  f"after attempt {attempt} — retrying in {JOIN_RETRY_WAIT}s")
                        time.sleep(JOIN_RETRY_WAIT)
                else:
                    with PRINT_LOCK:
                        print(f"  WARNING: [{team_key}] {member_box['name']} NOT joined to "
                              f"{domain} after {JOIN_ATTEMPTS} attempts")
            else:
                if _probe_joined(node, member_box, member_vmid, domain):
                    with PRINT_LOCK:
                        print(f"  [{team_key}] Linux member {member_box['name']} already joined to "
                              f"{domain} — skipping join (resume)")
                    continue
                with PRINT_LOCK:
                    print(f"  [{team_key}] Joining Linux member {member_box['name']} ({member_ip}) "
                          f"to {domain} via realmd/sssd...")
                for attempt in range(1, JOIN_ATTEMPTS + 1):
                    try:
                        with timed(comp_dir, 6, "domain_join", f"{team_key}/{member_box['name']}"):
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
                        with PRINT_LOCK:
                            print(f"  WARNING: [{team_key}] {member_box['name']} domain-join failed "
                                  f"— continuing ({str(e)[:160]})")
                    if _probe_joined(node, member_box, member_vmid, domain):
                        break
                    if attempt < JOIN_ATTEMPTS:
                        with PRINT_LOCK:
                            print(f"  [{team_key}] {member_box['name']} not joined to {domain} "
                                  f"after attempt {attempt} — retrying in {JOIN_RETRY_WAIT}s")
                        time.sleep(JOIN_RETRY_WAIT)
                else:
                    with PRINT_LOCK:
                        print(f"  WARNING: [{team_key}] {member_box['name']} NOT joined to "
                              f"{domain} after {JOIN_ATTEMPTS} attempts")

    # Each team's ADDS/join chain is team-local, so the chains run concurrently (M2.4)
    # while staying serial within a team (promotion must precede joins). Safe since M2.3:
    # every nakon pass stages into its own run dir, and bundle builds are serialized
    # operator-side by build_nakon_bundle's lock. Up to ~20 min of reboot-wait per DC
    # team collapses from a sum into a max.
    results = run_concurrent(list(teams.items()), _deploy_team_domains, max_workers=4)
    # run_concurrent never raises — a team chain that blew up mid-flight (ADDS planted
    # but misconfigs/joins skipped) used to vanish silently here and only surface as a
    # verify domains FAIL an hour later (live-found 2026-09-30). Surface every failure,
    # then fail the phase: resume re-enters per team via the promotion/marker probes.
    failures = [(item[0], r) for item, r in zip(list(teams.items()), results)
                if isinstance(r, Exception)]
    for team_key, exc in failures:
        with PRINT_LOCK:
            print(f"  ERROR: [{team_key}] domain chain failed: {str(exc)[:300]}")
    if failures:
        raise SystemExit(
            f"  ERROR: {len(failures)} team domain chain(s) failed (above). Resume with "
            f"--from-phase 6 — completed promotions are detected and skipped, the "
            f"remaining misconfigs/accounts/joins re-run per team.")
