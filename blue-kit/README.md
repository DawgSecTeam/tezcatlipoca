# blue-kit — the estate's hardening toolkit

Scripts the estate's own operators run on a competition's boxes, shipped to blue
as-is. **Run them from the box, not from here** — this directory is the kit's
source, and everything in it is safe to copy to a box.

## What's here

- `autofirewall.sh` — default-deny firewall from `port-sources/<box>` (INBOUND
  list). `-d` keeps package-manager ports open; `-c` clears the firewall.
  **The scored ports above must stay reachable** — after ANY firewall change
  run `../scorch` from the workdir and check every pin is still UP.
- `watchdawg.sh <backup-dir> <watchlist>` — file-integrity watcher. Run in the
  background (`nohup ./watchdawg.sh /var/lib/wd /etc/kernel/watchlist &`);
  it never exits on its own.
- `auditd-rules` — the auditd rule set (RHEL-shaped; a few rules fail to load
  on other distros — known noise).
- `auditkey-notifier.sh` — auditd key notifier.
- `baseline/standard.sh`, `baseline/specific.sh` — read-only audits. Run them
  for findings; they change nothing.

## NOT for the event

- `harden.sh` — replaces shells and locks accounts (`/bin/false` for every
  uid < 1000, rbash for the rest). It is the kit's DEPLOY tool, not an event
  tool: running it during the event breaks the scored ssh pins (the credlist
  accounts' shells become rbash, the credlist users' shells become /bin/false).
  Read it for ideas; never run it during the event.

## The scoring contract (unchanged)

- The scored pins log in as a credlist account WITH A PASSWORD from the
  gateway — never set `PasswordAuthentication no` globally, and keep the
  gateway-only exception if you touch sshd_config (`Match User <credlist-user>
  Address 192.168.<tid>.1` / `PasswordAuthentication yes`).
- After ANY firewall change: `../scorch` — every scored pin must stay UP.
