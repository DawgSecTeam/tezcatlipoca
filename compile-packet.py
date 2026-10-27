#!/usr/bin/env python3
"""Compile a packet profile (<comps-root>/<event>/packet.yaml) into a competition bundle.

The packet a competition publishes days ahead carries the general shape — boxes,
scored services, IP scheme, default credentials, schedule. Packets live beside their
competition definitions in the comps repo; this compiles that shape into
<comps-root>/<id>/ ready for `create-competition.py --competition <id>
--teams N --yes`. See docs/packet-profiles.md."""

import argparse
import sys
from config.packet_ops import (load_profile, packet_yaml_for_event, compile_profile,
                               resolve_packet_source)


def main():
    parser = argparse.ArgumentParser(
        description="Compile a packet profile into a deployable competition bundle.")
    parser.add_argument("profile", help="path to a packet.yaml (comp-dir/comps-root "
                        "relative, e.g. cde-2026/packet.yaml) or a bare event name")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate + print the plan and fidelity report, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing authored bundle in competitions/<id> "
                             "(deploy artifacts are left alone)")
    parser.add_argument("--competitions-dir", default=None,
                        help="override the comps root (default: the tezcatlipoca-competitions "
                             "repo — see docs)")
    args = parser.parse_args()

    profile_path = resolve_packet_source(args.profile)
    if profile_path is None or profile_path.is_dir():
        profile_path = packet_yaml_for_event(args.profile)  # bare event name
    if not profile_path.exists():
        print(f"ERROR: no packet profile at {args.profile} "
              "(path, comp-dir/comps-root relative, or an event name)", file=sys.stderr)
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
