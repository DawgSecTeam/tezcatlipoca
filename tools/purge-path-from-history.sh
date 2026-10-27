#!/usr/bin/env bash
# Purge a path from ALL git history, then verify it is really gone.
#
# The step everybody forgets is the last one: filter-branch leaves the blob alive under
# refs/original/* and in the reflog, so without dropping those and `gc --prune=now` the
# secret stays in the object store and stays cloneable. This script does all of it.
#
# Usage: purge-path-from-history.sh <path-relative-to-repo-root>
# Run from the repo root. TAKE A BUNDLE BACKUP FIRST:
#   git bundle create ../backup.bundle --all
#
# AFTERWARDS (manual, outside this script):
#   * rotate every credential that was exposed — purging history does NOT un-leak it
#   * force-push each rewritten branch:  git push --force-with-lease origin <branch>
#   * tell every collaborator to re-clone (their old clones still contain the secret)
set -euo pipefail

TARGET="${1:?usage: $0 <path-relative-to-repo-root>}"
cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: working tree is dirty. Commit or stash first — filter-branch rewrites refs" >&2
  echo "       underneath your checkout." >&2
  exit 1
fi

echo "==> verifying '$TARGET' is actually present before rewriting"
git log --oneline --all -- "$TARGET" | head -5 || true

# Capture the blobs BEFORE the rewrite: afterwards the path is gone from every tree, so the
# old "is it pruned?" check could only ever find nothing and always printed ok.
old_blobs=$(git rev-list --all | while read -r c; do
  git rev-parse "$c:$TARGET" 2>/dev/null || true
done | sort -u)
echo "==> ${#old_blobs} distinct blob(s) carry '$TARGET' today"

echo "==> rewriting all refs (this rewrites every commit that touched '$TARGET')"
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --prune-empty \
  --index-filter "git rm --cached --ignore-unmatch '$TARGET'" \
  --tag-name-filter cat -- --all

echo "==> dropping filter-branch safety backups (refs/original/*)"
git for-each-ref --format='%(refname)' refs/original | xargs -r -n1 git update-ref -d

echo "==> expiring reflogs and pruning unreachable objects"
git reflog expire --expire=now --all
git gc --prune=now

echo "==> VERIFICATION"
fail=0
for r in $(git for-each-ref --format='%(refname)' refs/heads refs/remotes refs/tags); do
  if git cat-file -e "$r:$TARGET" 2>/dev/null; then
    echo "  FAIL: $r still contains $TARGET"
    fail=1
  fi
done
[ "$fail" -eq 0 ] && echo "  ok: no ref contains $TARGET"

# Object-level check: the file may be gone from trees while its blob survives.
echo "  checking every blob that carried $TARGET is really gone..."
for blob in $old_blobs; do
  if git cat-file -e "$blob" 2>/dev/null; then
    echo "  FAIL: blob $blob still exists in the object store"
    fail=1
  fi
done
[ "$fail" -eq 0 ] && echo "  ok: all ${#old_blobs} old blob(s) pruned"

[ "$fail" -eq 0 ] && echo "==> PURGE COMPLETE — now rotate the credential and force-push" || {
  echo "==> PURGE INCOMPLETE"; exit 1; }
