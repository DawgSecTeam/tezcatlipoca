#!/usr/bin/env python3
"""Entry point for the tezcatlipoca deploy pipeline.

Thin CLI by design (it keeps the repo's dash-named entry-point shape): the hyphen makes
this filename unimportable as a module, so there is no library surface to expose here.
Consumers import pipeline_api instead. The previous build re-exported all 74 names it
happened to import so that redeploy-competition.py could load it via importlib — that
left redeploy's real dependency set invisible and let this file's surface drift silently.
"""

import deploy  # noqa: E402  (loads .env at import time)

if __name__ == "__main__":
    deploy.main()
