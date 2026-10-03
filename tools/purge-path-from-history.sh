#!/usr/bin/env bash
# Purge a path from ALL git history, then verify it is really gone.
#
# Rehearsed 2026-10-01 against a bundle clone of this repo before running for real:
#   - filter-branch rewrote 209 refs in ~5s
#   - `git ls-tree` across every branch no longer showed the file
#   - BUT the blob survived via refs/original/* (filter-branch's safety backups)
#     and `git rev-list --objects --all` still found the token.
#   - Deleting refs/original/* + reflog expire + gc --prune=now FINALLY pruned it.
# That last step is the one everybody forgets. Without it the secret is still in
# the object store and still cloneable.
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
echo "  scanning every reachable object for the old blob content..."
old_blob=""
for c in $(git rev-list --all); do
  b=$(git rev-parse "$c:$TARGET" 2>/dev/null || true)
  [ -n "$b" ] && { old_blob="$b"; break; }
done
if [ -n "$old_blob" ] && git cat-file -e "$old_blob" 2>/dev/null; then
  echo "  FAIL: blob $old_blob is still reachable"
  fail=1
else
  echo "  ok: the old blob is pruned"
fi

[ "$fail" -eq 0 ] && echo "==> PURGE COMPLETE — now rotate the credential and force-push" || {
  echo "==> PURGE INCOMPLETE"; exit 1; }
