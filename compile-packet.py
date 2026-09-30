#!/usr/bin/env python3
"""Compile a packet profile (packets/<event>/packet.yaml) into a competition bundle.

The packet a competition publishes days ahead carries the general shape — boxes,
scored services, IP scheme, default credentials, schedule. This compiles that shape
into competitions/<id>/ ready for `create-competition.py --competition <id>
--teams N --yes`. See docs/packet-profiles.md."""

import argparse
import sys
from pathlib import Path

from packet_ops import PACKETS_DIR, compile_profile, load_profile, validate_profile


def main():
    parser = argparse.ArgumentParser(
        description="Compile a packet profile into a deployable competition bundle.")
    parser.add_argument("profile", help="path to packets/<event>/packet.yaml (or an "
                                         f"event name under {PACKETS_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate + print the plan and fidelity report, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing authored bundle in competitions/<id> "
                             "(deploy artifacts are left alone)")
    parser.add_argument("--competitions-dir", default=None,
                        help="override the competitions/ root (default: repo competitions/)")
    args = parser.parse_args()

    profile_path = Path(args.profile)
    if not profile_path.exists():
        candidate = PACKETS_DIR / args.profile / "packet.yaml"
        if candidate.exists():
            profile_path = candidate
        else:
            print(f"ERROR: no profile at {profile_path} (or {candidate})", file=sys.stderr)
            return 2

    comp_dir, fidelity, wrote = compile_profile(
        profile_path, competitions_dir=args.competitions_dir,
        force=args.force, dry_run=args.dry_run)

    profile = load_profile(profile_path)
    print(f"\n  ── {'PLAN (dry run — nothing written)' if args.dry_run else 'Compiled'} "
          f"→ {comp_dir} " + "─" * 20)
    event = profile["event"]
    print(f"  event     : {event['name']} (difficulty {event['difficulty']})")
    print(f"  boxes     : {', '.join(b['name'] for b in profile['boxes'])}")
    services = profile.get("services") or []
    print(f"  services  : {len(services)} scored check(s) across "
          f"{len({s['box'] for s in services})} box(es)")
    if profile.get("injects"):
        print(f"  injects   : {len(profile['injects'])}")
    if args.dry_run:
        errors = validate_profile(profile)
        if errors:
            print("  VALIDATION ERRORS:")
            for e in errors:
                print(f"    - {e}")
            return 1
    if not args.dry_run:
        print("  wrote:")
        for path in wrote:
            print(f"    {path.relative_to(comp_dir.parent)}")
    print("\n" + fidelity)
    if not args.dry_run:
        print(f"  Next: python3 create-competition.py --competition {comp_dir.name} "
              f"--teams <N> --yes --scoring-vmid <free-vmid>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
