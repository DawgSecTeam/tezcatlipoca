#!/usr/bin/env python3
"""Post-deploy verifier for a Quotient scoring range: logins, services, isolation, misconfig spot-check, injects.

Thin CLI entrypoint: the gates live in the `verifier/` package (one module per concern).
The names re-exported below are the stable surface that tests and other scripts load this
file by path to reach; patch a gate or an SSH hop in its defining `verifier.*` module.
"""

import sys

try:
    import requests  # fail early with the install hint; re-exported for tests
    import urllib3
except ImportError as e:
    print(f"ERROR: missing dependency ({e}). Need: requests, python-dotenv.", file=sys.stderr)
    sys.exit(2)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from verifier.cli import main, run  # noqa: E402
from verifier.context import REPO_ROOT, build_ctx, read_terraform_ctx  # noqa: E402
from verifier.creds import check_no_default_creds, load_admin_password  # noqa: E402
from verifier.domains import check_domains  # noqa: E402
from verifier.engine import check_injects, check_round_loop, closed_injects  # noqa: E402
from verifier.freeze import do_freeze, do_unfreeze, git_dirty_lines  # noqa: E402
from verifier.isolation import check_isolation  # noqa: E402
from verifier.misconfig import check_misconfig, check_misconfig_survival  # noqa: E402
from verifier.model import (CheckError, GateResult, Status, bool_gate,  # noqa: E402
                            gate_fail, gate_pass, gate_skip)
from verifier.packet import check_packet_accounts, check_packet_creds  # noqa: E402
from verifier.red import check_red_identity, check_red_teams  # noqa: E402
from verifier.reports import report_beacons, report_healthcheck_status  # noqa: E402
from verifier.scoreboard import check_logins, check_services  # noqa: E402
from verifier.state_gates import check_degradations, check_plant_coverage  # noqa: E402
from verifier.verdict import RunBudget, gate_verdict, summary_lines  # noqa: E402

__all__ = [
    "requests", "main", "run", "REPO_ROOT", "build_ctx", "read_terraform_ctx",
    "check_no_default_creds", "load_admin_password", "check_domains", "check_injects",
    "check_round_loop", "closed_injects", "do_freeze", "do_unfreeze", "git_dirty_lines",
    "check_isolation", "check_misconfig", "check_misconfig_survival", "CheckError",
    "GateResult", "Status", "bool_gate", "gate_fail", "gate_pass", "gate_skip",
    "check_packet_accounts", "check_packet_creds", "check_red_identity", "check_red_teams",
    "report_beacons", "report_healthcheck_status", "check_logins", "check_services",
    "check_degradations", "check_plant_coverage", "RunBudget", "gate_verdict", "summary_lines",
]

if __name__ == "__main__":
    run()
