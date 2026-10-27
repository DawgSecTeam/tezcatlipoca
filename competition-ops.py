#!/usr/bin/env python3
"""Competition lifecycle driver: start / freeze / resume / end / status.

Thin CLI: the driver lives in `quotient/lifecycle_ops.py`, which `run-schedule.py` shares.
"""

import argparse
import os
import sys
from pathlib import Path

import urllib3
from dotenv import load_dotenv

from quotient import lifecycle_ops

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent
ACTIONS = ("start", "freeze", "resume", "end", "status")


def main():
    # Keep the 0600 state file private regardless of the operator's umask.
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--comp-dir", dest="comp_dir", required=True,
                        help="the competition dir (or a per-run workspace copy) holding "
                             "estate-state.json, credentials.txt and injects/")
    parser.add_argument("--engine-ip", default=None,
                        help="override the scoring-engine IP (default: the state document)")
    parser.add_argument("--scoring-password", default=None, dest="scoring_password",
                        help="override the scoring account password")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    comp_dir = Path(args.comp_dir).resolve()
    if not comp_dir.is_dir():
        sys.exit(f"ERROR: competition dir not found: {comp_dir}")

    if args.action == "start":
        return lifecycle_ops.cmd_start(args, comp_dir)
    if args.action == "freeze":
        return lifecycle_ops.cmd_freeze(args, comp_dir, pause=True)
    if args.action == "resume":
        return lifecycle_ops.cmd_freeze(args, comp_dir, pause=False)
    if args.action == "end":
        return lifecycle_ops.cmd_end(args, comp_dir)
    return lifecycle_ops.cmd_status(args, comp_dir)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
