#!/usr/bin/env python3
"""Entry point for the tezcatlipoca deploy pipeline.

Thin CLI by design (it keeps the repo's dash-named entry-point shape): the hyphen makes
this filename unimportable as a module, so there is no library surface to expose here.
Consumers import pipeline_api instead. The previous build re-exported all 74 names it
happened to import so that redeploy-competition.py could load it via importlib — that
left redeploy's real dependency set invisible and let this file's surface drift silently.
"""

from pathlib import Path

from dotenv import load_dotenv  # noqa: E402

# Load .env before deploy is imported: the pipeline reads TF_VAR_* and Proxmox creds from
# the environment at import time. deploy.py loads the same file at its own module scope,
# but owning it here keeps the entry point self-describing. load_dotenv does not overwrite
# variables that are already set, so the second load is a no-op.
load_dotenv(Path(".env"))

import deploy  # noqa: E402

if __name__ == "__main__":
    deploy.main()
