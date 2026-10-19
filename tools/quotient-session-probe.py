#!/usr/bin/env python3
"""Measure whether a second Quotient login evicts the first account's cookie.

Read-only: POSTs to /api/login and GETs /api/teams only. The matrix is the anchor
team twice, admin twice, admin-then-scoring (when the account exists), and the first
two team accounts. `first_cookie_after`/`second_cookie_after` are the HTTP status of
GET /api/teams made with that login's cookie.

  python3 tools/quotient-session-probe.py --base-url http://10.0.0.92 \
      --comp-dir competitions/<id> --json
  python3 tools/quotient-session-probe.py --base-url http://10.0.0.92 \
      --username team1 --password '<from credentials.txt>' --json

Exit 0 for any completed measurement (eviction or not); 2 on setup/login failure.
"""

import argparse
import json
import sys
from pathlib import Path

import requests
from requests.exceptions import RequestException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from estate import state as estate_state  # noqa: E402
from verifier.loaders import load_teams, read_credentials_lines  # noqa: E402
from verifier.model import CheckError  # noqa: E402

READ_PATH = "/api/teams"
TIMEOUT = 10


def _credential(lines, name):
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return None


def _accounts_from_comp_dir(comp_dir):
    comp_dir = Path(comp_dir)
    state = estate_state.load_flat(comp_dir)
    teams = load_teams(comp_dir) if (comp_dir / "teams.json").exists() else {}
    for name, data in (state.get("teams") or {}).items():
        teams.setdefault(name, {}).update(data)
    credentials = read_credentials_lines(comp_dir) or []
    return {
        "teams": {name: data.get("password") for name, data in teams.items()},
        "admin": state.get("admin_password") or _credential(credentials, "admin"),
        "scoring": state.get("scoring_password") or _credential(credentials, "scoring"),
    }


def _matrix_specs(accounts):
    specs, skipped = [], []
    team1 = accounts["teams"].get("team1")
    if team1:
        specs.append(("team1 twice", ("team1", team1), ("team1", team1)))
    else:
        skipped.append({"label": "team1 twice", "reason": "no team1 password"})

    admin = accounts["admin"]
    if admin:
        specs.append(("admin twice", ("admin", admin), ("admin", admin)))
    else:
        skipped.append({"label": "admin twice", "reason": "no admin password"})

    scoring = accounts["scoring"]
    if admin and scoring:
        specs.append(("admin then scoring", ("admin", admin), ("scoring", scoring)))
    elif not scoring:
        skipped.append({"label": "admin then scoring", "reason": "no scoring account"})
    else:
        skipped.append({"label": "admin then scoring", "reason": "no admin password"})

    team_keys = [name for name, password in accounts["teams"].items() if password]
    if len(team_keys) >= 2:
        first, second = team_keys[0], team_keys[1]
        specs.append((f"{first} then {second}",
                      (first, accounts["teams"][first]),
                      (second, accounts["teams"][second])))
    else:
        skipped.append({"label": "two team accounts",
                        "reason": "fewer than two team passwords"})
    return specs, skipped


def _login(base_url, username, password):
    response = requests.post(f"{base_url}/api/login",
                             json={"username": username, "password": password},
                             timeout=TIMEOUT)
    cookie = "; ".join(f"{name}={value}"
                       for name, value in response.cookies.get_dict().items())
    return response.status_code, cookie


def _cookie_status(base_url, cookie):
    response = requests.get(f"{base_url}{READ_PATH}",
                            headers={"Cookie": cookie}, timeout=TIMEOUT)
    return response.status_code


def _accepted(status):
    return status is not None and 200 <= status < 300


def _probe_row(base_url, label, first, second):
    first_login, first_cookie = _login(base_url, *first)
    second_login, second_cookie = _login(base_url, *second)
    first_after = _cookie_status(base_url, first_cookie) if first_cookie else None
    second_after = _cookie_status(base_url, second_cookie) if second_cookie else None
    evicted = bool(first_login < 400 and second_login < 400 and first_cookie
                   and first_after is not None and not _accepted(first_after)
                   and _accepted(second_after))
    return {
        "label": label,
        "first_login_status": first_login,
        "second_login_status": second_login,
        "first_cookie_after": first_after,
        "second_cookie_after": second_after,
        "evicted": evicted,
    }


def _print_human(result):
    print(f"base URL: {result['base_url']}   read-only endpoint: GET {result['endpoint']}")
    print(f"{'label':22} {'first':>5} {'second':>6} {'cookie A':>8} {'cookie B':>8}  evicted")
    for row in result["rows"]:
        first_after = "-" if row["first_cookie_after"] is None else row["first_cookie_after"]
        second_after = "-" if row["second_cookie_after"] is None else row["second_cookie_after"]
        print(f"{row['label']:22} {row['first_login_status']:>5} "
              f"{row['second_login_status']:>6} {first_after!s:>8} {second_after!s:>8}  "
              f"{row['evicted']}")
    summary = result["summary"]
    evicted = ", ".join(summary["evicted_labels"]) or "none"
    print(f"\nresult: {summary['result']} (evicted: {evicted})")
    for skip in summary["skipped"]:
        print(f"  skipped {skip['label']}: {skip['reason']}")
    print("Rerun (secrets are never printed; supply the password from credentials.txt):")
    for command in result["rerun"]:
        print(f"  {command}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True,
                        help="Quotient base URL, e.g. http://10.0.0.92")
    parser.add_argument("--comp-dir", default=None,
                        help="competition dir: read teams.json / estate state / credentials.txt")
    parser.add_argument("--username", default=None,
                        help="single account to probe twice (no --comp-dir)")
    parser.add_argument("--password", default=None,
                        help="that account's password")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    if args.comp_dir and (args.username or args.password):
        parser.error("use --comp-dir or --username/--password, not both")
    if not args.comp_dir and not (args.username and args.password):
        parser.error("--username and --password are required without --comp-dir")

    base_url = args.base_url.rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        base_url = f"http://{base_url}"

    try:
        if args.comp_dir:
            accounts = _accounts_from_comp_dir(args.comp_dir)
            specs, skipped = _matrix_specs(accounts)
            rerun = [f"python3 tools/quotient-session-probe.py --base-url {base_url} "
                     f"--comp-dir {Path(args.comp_dir)} --json"]
        else:
            specs = [(f"{args.username} twice",
                      (args.username, args.password),
                      (args.username, args.password))]
            skipped = []
            rerun = [f"python3 tools/quotient-session-probe.py --base-url {base_url} "
                     f"--username {args.username} --password '<password>' --json"]
        rows = [_probe_row(base_url, label, first, second)
                for label, first, second in specs]
    except RequestException as e:
        print(f"ERROR: could not reach {base_url}: {e}", file=sys.stderr)
        return 2
    except (CheckError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if not rows:
        print("ERROR: no account pairs to probe — check the comp dir or credentials",
              file=sys.stderr)
        return 2

    login_failures = [row["label"] for row in rows
                      if row["first_login_status"] >= 400
                      or row["second_login_status"] >= 400]
    evicted = [row["label"] for row in rows if row["evicted"]]
    result = {
        "base_url": base_url,
        "endpoint": READ_PATH,
        "cookie_test": f"GET {READ_PATH} with that login's Cookie header; 2xx = accepted",
        "rows": rows,
        "summary": {
            "result": ("login-failure" if login_failures else
                       "eviction-observed" if evicted else "no-eviction"),
            "eviction_observed": bool(evicted),
            "evicted_labels": evicted,
            "login_failures": login_failures,
            "skipped": skipped,
        },
        "rerun": rerun,
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)
    return 2 if login_failures else 0


if __name__ == "__main__":
    sys.exit(main())
