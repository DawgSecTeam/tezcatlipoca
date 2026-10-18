#!/usr/bin/env python3
"""Thin CLI over template_sync_ops (keeps the repo's dash-named entry-point shape)."""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

if not os.environ.get("TEZ_TEST_OFFLINE"):
    load_dotenv(Path(__file__).resolve().parent / ".env")

from multinode import template_sync_ops  # noqa: E402

if __name__ == "__main__":
    template_sync_ops.main()
