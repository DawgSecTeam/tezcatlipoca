#!/usr/bin/env python3
"""Post-deploy verifier for a Quotient scoring range: logins, services, isolation, misconfig spot-check, injects.

Thin CLI entrypoint: the gates live in the `verifier/` package (one module per concern).
"""

import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from verifier.cli import run  # noqa: E402

if __name__ == "__main__":
    run()
