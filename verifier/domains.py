"""AD domain gate: DC serving the team domain, valid unique DomainSIDs, every member joined."""

import json
import os

from domain_ops import team_domain
from range_ops import guest_agent_exec_root, guest_agent_exec_windows, vm_id_for
from utils import MAX_CONCURRENCY, run_concurrent
from windows_ops import is_windows_template

from verifier import context
from verifier.model import CheckError, gate_fail, gate_pass, gate_skip


_WIN_DOMAIN_PS = (
    "$cs = Get-WmiObject Win32_ComputerSystem; "
    "'HOST=' + $env:COMPUTERNAME; "
    "'ROLE=' + $cs.DomainRole; 'DOMAIN=' + $cs.Domain; 'PARTOF=' + $cs.PartOfDomain; "
    "'MSID=' + (New-Object System.Security.Principal.NTAccount('Administrator'))"
    ".Translate([System.Security.Principal.SecurityIdentifier]).AccountDomainSid.Value; "
    "if ($cs.DomainRole -ge 4) { try { $d = Get-ADDomain -ErrorAction Stop; "
    "'DSID=' + $d.DomainSID.Value; 'DNSROOT=' + $d.DNSRoot; "
    "'SVC=' + [bool](Get-ADUser -Filter \"SamAccountName -eq 'svc-support'\" -ErrorAction Stop); "
    "'PCS=' + ((Get-ADComputer -Filter * | Select-Object -ExpandProperty Name) -join ',') "
    "} catch { 'ADERR=' + $_.Exception.Message } }"
)


def _kv(out):
    return dict(l.split("=", 1) for l in (out or "").splitlines() if "=" in l)


def _valid_domain_sid(sid):
    """S-1-5-21-<a>-<b>-<c> — a DOMAIN SID has exactly three sub-authorities and no
    trailing RID (an 8-part value is an account SID, not a domain SID)."""
    parts = (sid or "").split("-")
    return (len(parts) == 7 and sid.startswith("S-1-5-21-")
            and all(p.isdigit() for p in parts[3:]))


def _member_trusted(job, kv, pcs_by_team):
    """True/False/None: does the team's DC hold a machine account for this member?

    None = cannot judge (the DC's listing never arrived, or the member reported no
    hostname) — the gate then leans on the member-side check alone rather than
    inventing a verdict. False = the member self-reports joined but the DC that must
    hold its machine account does not: a dead trust, never a pass."""
    if job["role"] != "member" or not pcs_by_team:
        return None
    accounts = pcs_by_team.get(job["team_key"])
    if accounts is None:
        return None
    host = (kv.get("HOST") or "").strip().upper() if kv else ""
    if not host:
        return None
    return host in accounts


def check_domains(comp_dir, teams, boxes, ctx=None):
    """Domain gate (replaces the freeze's operator attestation with a live check).

    Per team: the DC answers Get-ADDomain for team<id>.local with a syntactically
    valid DomainSID, the planted AD misconfig (svc-support) exists, and every member
    box is actually joined. Across teams: DomainSIDs must be unique (a collision
    means DC promotion reused image state — winad-testrun 2026-09-25 found exactly
    that); with a single team uniqueness cannot be exercised, so the PASS says so
    instead of claiming it. Member machine SIDs are reported, not gated: members
    linked-cloned from one golden share them by design, which is harmless for
    isolated forests. A present-but-malformed domain_roles.json fails closed.
    Returns a SKIP GateResult (gating=False) when the lineup has no domain_roles.json."""
    roles_path = comp_dir / "domain_roles.json"
    if not roles_path.exists():
        print("  SKIP  — no domain_roles.json")
        return gate_skip("domains", "no domain_roles.json", gating=False)
    try:
        roles = json.loads(roles_path.read_text())
    except (OSError, ValueError) as e:
        print(f"  FAIL  domain_roles.json is unreadable/malformed ({str(e)[:80]})")
        return gate_fail("domains", "domain_roles.json unreadable/malformed")
    if not isinstance(roles, dict) or not all(
            isinstance(name, str) and isinstance(role, str)
            for name, role in roles.items()):
        print("  FAIL  domain_roles.json must map box names to 'dc' or 'member' strings")
        return gate_fail("domains", "domain_roles.json schema invalid")
    bad = {name: role for name, role in roles.items() if role not in ("dc", "member")}
    if bad:
        print("  FAIL  domain_roles.json has invalid role value(s): "
              + ", ".join(f"{name}={role!r}" for name, role in sorted(bad.items()))
              + " (expected 'dc' or 'member')")
        return gate_fail("domains", "domain_roles.json has invalid role values")
    node = os.environ.get("TF_VAR_proxmox_node")
    # Box TYPES (boxes.json order = vmid order), not verify's per-team nakon machines.
    try:
        boxes = json.loads((comp_dir / "boxes.json").read_text())
    except (OSError, ValueError) as e:
        print(f"  FAIL  boxes.json is unreadable/malformed ({str(e)[:80]})")
        return gate_fail("domains", "boxes.json unreadable/malformed")
    idx = {b["name"]: i for i, b in enumerate(boxes)}
    unknown = [name for name in roles if name not in idx]
    if unknown:
        print("  FAIL  domain_roles.json names box(es) absent from boxes.json: "
              + ", ".join(sorted(unknown)))
        return gate_fail("domains", "domain_roles.json names unknown box(es)")
    if not teams:
        print("  FAIL  no teams loaded — nothing to check domain roles against")
        return gate_fail("domains", "no teams loaded")
    dc_name = next((n for n, r in roles.items() if r == "dc"), None)
    ok = True
    domain_sids, machine_sids = {}, {}

    # Work units in the serial loop's exact order (sorted teams, then `roles` order).
    # Each unit is one independent per-VM probe: a Windows guest-agent call with a 120s
    # timeout, or a Linux guest-agent call with a 60s timeout plus a 60s SSH fallback.
    # For 4 teams x 5 boxes that was ~20 SEQUENTIAL probes — 3-10 minutes realistically
    # and up to ~40 minutes in the all-timeout case, which is precisely the
    # half-deployed range this gate exists to catch. Bound MAX_CONCURRENCY (8), not the
    # per-box VM-work bound of 4: these are not Proxmox tasks (each VM has its own
    # virtio-serial agent channel), and utils.run_concurrent's docstring pins 8 as the
    # load-bounded cap that stays far under sshd's raised MaxSessions (64).
    jobs = []
    for team_key, team in sorted(teams.items()):
        ident = team["identifier"]
        domain = team_domain(comp_dir, ident)
        for name, role in roles.items():
            jobs.append({
                "team_key": team_key, "ident": ident, "domain": domain,
                "name": name, "role": role,
                "vmid": vm_id_for(ident, idx[name]),
                "windows": is_windows_template(boxes[idx[name]].get("template") or ""),
            })

    def _probe(job):
        """One box's probe, returning an outcome instead of printing it.

        The main thread then walks `jobs` in the serial order and aggregates, so every
        verdict, every printed line, and (critically) the team order inside the
        duplicate-DomainSID message stay exactly what the serial loop produced — the
        2026-10-02 tri-state gate contract: same status, same message, same exit code.
        """
        team_key, name = job["team_key"], job["name"]
        ident, domain = job["ident"], job["domain"]
        if job["windows"]:
            try:
                _rc, out, err = guest_agent_exec_windows(node, job["vmid"], _WIN_DOMAIN_PS,
                                                         timeout=120)
                return {"kv": _kv(out), "err": err or "", "info": [], "fail": ""}
            except Exception as e:
                return {"kv": None, "err": "", "info": [],
                        "fail": f"  FAIL  {team_key}/{name}: guest-agent probe failed "
                                f"({str(e)[:80]})"}
        realm_cmd = (f"echo HOST=$(hostname -s); "
                     f"realm list 2>/dev/null | grep -qi 'domain-name: *{domain}' "
                     f"&& echo JOINED=1 || echo JOINED=0")
        try:
            _rc, out, err = guest_agent_exec_root(node, job["vmid"], realm_cmd, timeout=60)
            return {"kv": _kv(out), "err": err or "", "info": [], "fail": ""}
        except Exception as agent_err:
            # The PVE agent channel has a per-instance exec breaker that can stay
            # tripped (amongus-cde 2026-09-30: airship's failed join probes tripped it
            # permanently). Linux boxes are still reachable over gateway SSH — fall
            # back to it before failing the gate.
            try:
                box_ip = f"192.168.{ident}.{boxes[idx[name]]['last_octet']}"
                proc = context.ssh_via_gateway(ctx or {"ssh_key_path": str(context.resolve_ssh_key())},
                                       box_ip, realm_cmd, timeout=60)
                kv = _kv(proc.stdout)
                if kv.get("JOINED") is None:
                    raise CheckError(f"unparseable realm probe: {proc.stdout[:80]}")
                return {"kv": kv, "err": "",
                        "info": [f"  INFO  {team_key}/{name}: agent channel unavailable, "
                                 f"probed over gateway SSH"],
                        "fail": ""}
            except Exception as ssh_err:
                return {"kv": None, "err": "", "info": [],
                        "fail": f"  FAIL  {team_key}/{name}: guest-agent probe failed "
                                f"({str(agent_err)[:80]}) and gateway-SSH fallback failed "
                                f"({str(ssh_err)[:60]})"}

    probe_results = run_concurrent(jobs, _probe, max_workers=MAX_CONCURRENCY)

    # DC-side trust map (live-found 2026-10-03, reset matrix): a member can self-report
    # joined while the DC's freshly re-promoted AD holds NO machine account for it —
    # Add-Computer then refuses any re-join ("already in that domain") and the dead
    # trust is invisible to the member-side probe. The DC's Get-ADComputer listing is
    # the truth; every member's hostname must appear in it.
    pcs_by_team = {}
    for job, outcome in zip(jobs, probe_results):
        kv = outcome.get("kv") if isinstance(outcome, dict) else None
        if kv and job["role"] == "dc" and kv.get("PCS") is not None:
            pcs_by_team[job["team_key"]] = {
                n.strip().upper() for n in kv["PCS"].split(",") if n.strip()}

    for job, outcome in zip(jobs, probe_results):
        team_key, name, role = job["team_key"], job["name"], job["role"]
        domain, windows = job["domain"], job["windows"]
        if isinstance(outcome, Exception):
            # Every probe-body exception was already caught below into a FAIL line;
            # only something outside those handlers could escape, and it still does.
            raise outcome
        for line in outcome["info"]:
            print(line)
        if outcome["fail"]:
            print(outcome["fail"])
            ok = False
            continue
        # `err` is this box's own stderr. The serial loop could leak the PREVIOUS
        # iteration's `err` into the DC detail line when the guest-agent call raised and
        # the SSH fallback then succeeded (the tuple assignment never happened); that
        # leak is not reproduced — it printed another box's stderr, and the verdict in
        # that path is FAIL either way.
        kv, err = outcome["kv"], outcome["err"]
        if role == "dc":
            if kv.get("ADERR") or kv.get("DNSROOT", "").lower() != domain:
                print(f"  FAIL  {team_key}/{name}: DC not serving {domain} "
                      f"({kv.get('ADERR') or kv.get('DNSROOT') or (err or '').strip()[:80]})")
                ok = False
                continue
            dsid = kv.get("DSID") or ""
            if not _valid_domain_sid(dsid):
                print(f"  FAIL  {team_key}/{name}: {domain} reports no valid "
                      f"DomainSID ({dsid or 'DSID missing'} — a promoted DC must "
                      f"answer Get-ADDomain with an S-1-5-21-* SID)")
                ok = False
                continue
            domain_sids.setdefault(dsid, []).append(team_key)
            svc = kv.get("SVC", "").lower() == "true"
            print(f"  {'PASS' if svc else 'FAIL'}  {team_key}/{name}: {domain} "
                  f"DomainSID {dsid}; svc-support {'present' if svc else 'MISSING'}")
            ok &= svc
        elif windows:
            joined = kv.get("PARTOF", "").lower() == "true" and kv.get("DOMAIN", "").lower() == domain
            trusted = _member_trusted(job, kv, pcs_by_team)
            verdict = "joined" if joined else "NOT joined"
            detail = ""
            if trusted is False:
                verdict += "; DC has NO machine account for it"
                detail = " — dead trust (the DC was re-promoted; the member self-report is stale)"
            elif trusted is True and joined:
                detail = "; machine account confirmed on the DC"
            print(f"  {'PASS' if joined and trusted is not False else 'FAIL'}  "
                  f"{team_key}/{name}: {verdict} to {domain}{detail}")
            ok &= joined and trusted is not False
            machine_sids.setdefault(kv.get("MSID"), []).append(f"{team_key}/{name}")
        else:
            joined = kv.get("JOINED") == "1"
            trusted = _member_trusted(job, kv, pcs_by_team)
            verdict = "realm-joined" if joined else "NOT realm-joined"
            if trusted is False:
                verdict += "; DC has NO machine account for it"
            print(f"  {'PASS' if joined and trusted is not False else 'FAIL'}  "
                  f"{team_key}/{name}: {verdict} to {domain}")
            ok &= joined and trusted is not False
    dupes = {sid: t for sid, t in domain_sids.items() if len(t) > 1}
    if dupes:
        for sid, t in dupes.items():
            print(f"  FAIL  DomainSID {sid} shared by {', '.join(t)} — DC promotion reused "
                  f"image state (the DC box type must use an unbooted golden)")
        ok = False
    elif domain_sids and dc_name:
        if len(teams) > 1:
            print(f"  PASS  {len(domain_sids)} team domain(s), all DomainSIDs unique")
        else:
            print("  PASS  1 team domain, DomainSID well-formed "
                  "(uniqueness needs a second team to exercise)")
    shared = {sid: v for sid, v in machine_sids.items() if len(v) > 1}
    for sid, v in shared.items():
        print(f"  INFO  member machine SID {sid} shared by {', '.join(v)} "
              f"(linked clones of one golden — harmless for isolated forests)")
    if not ok:
        return gate_fail("domains", "domain validation failures above")
    return gate_pass("domains", f"{len(domain_sids)} team domain(s) validated")
