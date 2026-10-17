"""`python3 -m tlaloc` — the loop's own entry point (the systemd unit runs this)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
