"""Vocabulary shared by every artifacts_lib module: file names, statuses, kinds."""

import re
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent

SCHEMA = 1
DIRNAME = ".automated-tests"
INDEX_NAME = "index.json"
MANIFEST_NAME = "test.json"
COLLECTION_NAME = "collection.json"
REPORT_NAME = "REPORT.md"
KINDS = ("scrim", "deploy", "soak", "canary", "loadtest", "rehearsal")

# Side name -> canonical document in the test folder.
SIDES = {"red": "RED-TEAM.md", "blue": "BLUE-TEAM.md"}
EVIDENCE_DIRS = ("red", "engine", "blue", "harness")

OK = "ok"
SEALED = "sealed"
ABSENT = "absent"
FAILED = "failed"
UNREACHABLE = "unreachable"
UNRECOVERABLE = "unrecoverable"
SKIPPED = "skipped"
# A stub-only status, deliberately outside the collection vocabulary: the sources exist but
# nobody ran the collector, so "absent" (a fact about the box) would be a lie about the folder.
NOT_COLLECTED = "not-collected"
STATUSES = (OK, SEALED, ABSENT, FAILED, UNREACHABLE, UNRECOVERABLE, SKIPPED)
# "we wanted this and do not have it" — the only statuses allowed to raise a warning.
LOST = (FAILED, UNREACHABLE, UNRECOVERABLE)

RUN_ID_RE = re.compile(r"^run-[0-9a-f]{8}$")
TODO_MARK = "<!-- TODO(author)"


class Unreachable(Exception):
    """The source exists on paper but cannot be talked to (VM stopped, agent down).

    Distinct from OSError/FileNotFoundError so the collector records `unreachable` instead of
    blaming the file for a dead box."""
