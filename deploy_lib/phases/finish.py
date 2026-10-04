"""Between-phase terraform context read, and the end-of-deploy credentials/summary output."""

import os
import time
from pathlib import Path

from config_ops import write_text_atomic
from ssh_ops import read_terraform_ctx
from timing import print_timing_summary


def connect_terraform(ctx):
    """Read terraform's outputs into the context (the SSH/engine coordinates).

    Deliberately between phase 2 and phase 3, and deliberately outside every
    phase's from_phase guard: a fresh run only has these outputs after apply #1,
    and a resume from phase 3+ never runs the apply at all. A failure here must
    be reported as the phase the operator asked to resume at, which is why the
    sequencer does not set current_phase for it."""
    ctx.tf_ctx = read_terraform_ctx(ctx.comp_dir)
    ctx.ssh_key = Path(ctx.tf_ctx["ssh_key_path"])
    ctx.scoring_user = os.environ["TF_VAR_vm_username"]
    ctx.scoring_ip = ctx.tf_ctx["scoring_engine_ip"]


def write_credentials_file(ctx):
    """credentials.txt: the operator/packet-facing credential file, 0600 at creation
    (the same rule as teams.json and .deploy_state.json)."""
    cred_lines = [
        f"# Credentials for {ctx.name} — generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Scoreboard:  http://{ctx.scoring_ip}",
        f"admin  {ctx.admin_password}",
    ]
    if getattr(ctx, "scoring_password", ""):
        cred_lines.append("# automation account: a SECOND admin, so a scheduled login "
                          "(round-loop watchdog, unattended verify) never evicts the "
                          "operator's admin session — Quotient allows one per account")
        cred_lines.append(f"scoring  {ctx.scoring_password}")
    if ctx.packet_pw:
        cred_lines.append("# box credentials below are the packet-published defaults "
                          "(passwords.json) — teams rotate them at minute zero")
    if ctx.inject_password:
        cred_lines.append(f"inject  {ctx.inject_password}")
    for team_name, team_data in ctx.teams.items():
        cred_lines.append(f"{team_name}  {team_data['password']}  (192.168.{team_data['identifier']}.0/24)")
    cred_lines.append(f"box-login ({ctx.box_username})  {ctx.box_password}")
    for user, pw in ctx.box_creds.items():
        cred_lines.append(f"box-credlist-{user}  {pw}")
    for user, pw in (ctx.domain_creds or {}).items():
        cred_lines.append(f"box-credlist-domain-{user}  {pw}")
    cred_path = ctx.comp_dir / "credentials.txt"
    # The operator/packet-facing credential file — same 0600-at-creation rule.
    write_text_atomic(cred_path, "\n".join(cred_lines) + "\n")


def print_live_summary(ctx):
    """The "is live" report: the only place the generated secrets are printed."""
    print(f"\n{'='*60}")
    print(f"  {ctx.name} is live")
    print(f"{'='*60}")
    print(f"Scenario: {ctx.scenario}")
    print(f"Saved to: competitions/{ctx.comp_name}/  (credentials.txt, mode 0600)")
    print(f"\nScoreboard:    http://{ctx.scoring_ip}")
    print(f"Admin login:   admin / {ctx.admin_password}")
    if getattr(ctx, "scoring_password", ""):
        print(f"Scoring login: scoring / {ctx.scoring_password}   (automation; separate "
              f"session from admin)")
    if ctx.inject_password:
        print(f"Inject login:  inject / {ctx.inject_password}   ({len(ctx.injects)} inject(s) loaded)")
    print("\nTeam logins:")
    for team_name, team_data in ctx.teams.items():
        print(f"  {team_name} / {team_data['password']}  (subnet 192.168.{team_data['identifier']}.0/24)")
    print(f"\nBox login:     {ctx.box_username} / {ctx.box_password}  (every team box)")
    print("Box credlist:  " + ", ".join(f"{u}/{p}" for u, p in ctx.box_creds.items()))
    print(f"\nScoring engine SSH: ssh -i {ctx.ssh_key} {ctx.scoring_user}@{ctx.scoring_ip}")
    print(f"{'='*60}")


def finish_deploy(ctx):
    """Write credentials.txt, the timing summary, and the "is live" report.

    The last thing deploy() does."""
    write_credentials_file(ctx)
    print_timing_summary(ctx.comp_dir)
    print_live_summary(ctx)
