"""Per-run test artifacts under a competition directory (see docs/automated-test-artifacts.md)."""

# Thin facade: the implementation is one concern per module in the `artifacts_lib` package.
# Callers keep using `import artifacts_ops` and patching names here; do not move logic back
# into this file. `subprocess`/`shutil` are re-exported because tests patch them through this
# module.

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
    DIRNAME,
    INDEX_NAME,
    MANIFEST_NAME,
    COLLECTION_NAME,
    REPORT_NAME,
    SIDES,
    EVIDENCE_DIRS,
    OK,
    SEALED,
    ABSENT,
    FAILED,
    UNREACHABLE,
    UNRECOVERABLE,
    SKIPPED,
    STATUSES,
    LOST,
    Unreachable,
)
from artifacts_lib.env import (
    in_worktree,
)
from artifacts_lib.hashing import (
    seal_local_file,
    sha256_file,
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
    next_run_key,
    artifacts_root,
    run_id_from_state,
    test_dir,
    test_key,
)
from artifacts_lib.plan import (
    plan_targets,
)
from artifacts_lib.report import (
    write_report_skeleton,
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
    'DIRNAME',
    'INDEX_NAME',
    'MANIFEST_NAME',
    'COLLECTION_NAME',
    'REPORT_NAME',
    'SIDES',
    'EVIDENCE_DIRS',
    'OK',
    'SEALED',
    'ABSENT',
    'FAILED',
    'UNREACHABLE',
    'UNRECOVERABLE',
    'SKIPPED',
    'STATUSES',
    'LOST',
    'Unreachable',
    'in_worktree',
    'seal_local_file',
    'sha256_file',
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
    'run_id_from_state',
    'test_dir',
    'test_key',
    'next_run_key',
    'plan_targets',
    'write_report_skeleton',
    'seal_test',
    'verify_test',
    'warn_summary',
    '_ssh_capture',
    'default_transport',
    'ingest_verdict',
]
