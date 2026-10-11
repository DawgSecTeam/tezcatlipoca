"""Per-run test artifacts under a competition directory.

The contract (design + rationale: docs/reports/automated-test-artifacts-plan-2026-10-03.md;
canonical prose lands in docs/automated-test-artifacts.md with step 7 of that plan):

    competitions/<comp>/.automated-tests/
      index.json                 roll-up of every test, newest first
      <key>/                     key = the deploy's run-<8hex>, else untagged-<ts>
        test.json                identity + intent + verdict + writeup status
        collection.json          what was pulled, from where, sha256, per-target status
        RED-TEAM.md              pulled from red01 (or an honest stub saying why not)
        BLUE-TEAM.md             blue's authored report, sealed (or a stub)
        REPORT.md                synthesis: incidents, run success, recommendations
        evidence/{red,engine,blue,harness}/

Why this is a module and not logic inside destroy-competition.py: **two callers must agree
exactly.** The scrim harness collects before `badauto destroy` erases red01; teardown collects
when the harness never got there (crash, kill, session death). A second copy of the pull logic
is how those two drift apart — tests/test_helper_dedup.py exists because that keeps happening.

Why the folder is keyed on the run id: it already exists (utils.mint_run_id), it is persisted in
.deploy_state.json, it is stamped on every VM as an ownership tag, it is reused across
resume/redeploy, and every destruction path already requires it (constants.ownership_tags). Two
worktrees running the same competition ID therefore cannot collide, and the artifact folder
correlates with VM identity for free. Older comp dirs have no run id — hence the stable
`untagged-<deploy timestamp>` fallback in test_key().

Statuses are deliberately a small closed set, because the thing this feature exists to prevent is
an empty folder that could mean anything:

    ok             fetched successfully
    sealed         local file copied, hashed, chmod 0600
    absent         the source was reachable and the file genuinely is not there
    failed         the source was reachable and the transfer failed
    unreachable    the source (VM/agent) could not be reached at all
    unrecoverable  the source is gone for good (VM already destroyed)
    skipped        not applicable to this run (e.g. no red agent in a plain deploy test)

Nothing here blocks a teardown. A missing report is printed loudly and recorded; destroying a
range is never held hostage by a dead guest box (operator decision, 2026-10-03).
"""

# Thin facade. The implementation lives in the `artifacts_lib` package, one concern per module:
#   constants  vocabulary (file names, statuses, kinds)      env        timestamps, git, worktree
#   paths      test-folder location + key                    manifest   test.json read/write
#   hashing    sha256, 0600 sealing, atomic bytes            plan       what to collect (pure)
#   transport  scp / guest-agent / local / ssh-cmd           collect    collector + collection.json
#   verdict    INTERACTION.md -> machine verdict             status     document state + warnings
#   report     stubs + REPORT.md skeleton                    index      index.json roll-up
#   archive    durable verified copies                       seal       verify + seal write-up
#   lifecycle  finalize + collect_for_teardown
# Callers (destroy-competition.py, run-agent-scrim.py, test-artifacts.py, tests) keep using
# `import artifacts_ops` and patching names here; do not move logic back into this file.
# `subprocess`/`shutil` are re-exported because tests patch them through this module (they are
# the same module objects the package uses).

import shutil
import subprocess

from artifacts_lib.archive import (
    archive_test,
    default_archive_root,
)
from artifacts_lib.collect import (
    collect,
    load_collection,
)
from artifacts_lib.constants import (
    REPO,
    SCHEMA,
    DIRNAME,
    INDEX_NAME,
    MANIFEST_NAME,
    COLLECTION_NAME,
    REPORT_NAME,
    KINDS,
    SIDES,
    EVIDENCE_DIRS,
    OK,
    SEALED,
    ABSENT,
    FAILED,
    UNREACHABLE,
    UNRECOVERABLE,
    SKIPPED,
    NOT_COLLECTED,
    STATUSES,
    LOST,
    RUN_ID_RE,
    TODO_MARK,
    Unreachable,
)
from artifacts_lib.env import (
    git_facts,
    in_worktree,
)
from artifacts_lib.hashing import (
    file_facts,
    seal_local_file,
    sha256_file,
    write_bytes_atomic,
)
from artifacts_lib.index import (
    list_tests,
    update_index,
)
from artifacts_lib.lifecycle import (
    collect_for_teardown,
    finalize,
)
from artifacts_lib.manifest import (
    ensure_test,
    load_manifest,
    record_paths,
    record_phase,
    save_manifest,
    update_manifest,
)
from artifacts_lib.paths import (
    latest_run_key,
    next_run_key,
    run_folders,
    artifacts_root,
    comp_name,
    run_id_from_state,
    test_dir,
    test_key,
)
from artifacts_lib.plan import (
    plan_targets,
    want_label,
)
from artifacts_lib.report import (
    render_report_skeleton,
    write_report_skeleton,
    write_stub,
)
from artifacts_lib.seal import (
    seal_test,
    verify_test,
)
from artifacts_lib.status import (
    warn_summary,
)
from artifacts_lib.transport import (
    _ssh_capture,
    default_transport,
)
from artifacts_lib.verdict import (
    ingest_verdict,
)

__all__ = [
    'shutil',
    'subprocess',
    'archive_test',
    'default_archive_root',
    'collect',
    'load_collection',
    'REPO',
    'SCHEMA',
    'DIRNAME',
    'INDEX_NAME',
    'MANIFEST_NAME',
    'COLLECTION_NAME',
    'REPORT_NAME',
    'KINDS',
    'SIDES',
    'EVIDENCE_DIRS',
    'OK',
    'SEALED',
    'ABSENT',
    'FAILED',
    'UNREACHABLE',
    'UNRECOVERABLE',
    'SKIPPED',
    'NOT_COLLECTED',
    'STATUSES',
    'LOST',
    'RUN_ID_RE',
    'TODO_MARK',
    'Unreachable',
    'git_facts',
    'in_worktree',
    'file_facts',
    'seal_local_file',
    'sha256_file',
    'write_bytes_atomic',
    'list_tests',
    'update_index',
    'collect_for_teardown',
    'finalize',
    'ensure_test',
    'load_manifest',
    'record_paths',
    'record_phase',
    'save_manifest',
    'update_manifest',
    'artifacts_root',
    'comp_name',
    'run_id_from_state',
    'test_dir',
    'test_key',
    'next_run_key',
    'latest_run_key',
    'run_folders',
    'plan_targets',
    'want_label',
    'render_report_skeleton',
    'write_report_skeleton',
    'write_stub',
    'seal_test',
    'verify_test',
    'warn_summary',
    '_ssh_capture',
    'default_transport',
    'ingest_verdict',
]
