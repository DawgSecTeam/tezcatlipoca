#!/bin/bash
# Convenience wrapper: runs create-competition.py from the repo root and tees output to a
# gitignored log. create-competition.py is stdin-driven (it ignores argv), so drive it with
# a printf feed. The numbers are answers to the interactive prompts, IN ORDER:
#   <competition menu number>  <how many teams>  <y to confirm>
# The menu number is competition-specific (see the "Previous competitions" list the script
# prints); adjust it for your competition. Example below reuses menu entry 1 with 2 teams.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

LOG="$REPO_DIR/deploy.log"   # gitignored (see .gitignore: deploy.log / deploy-*.log)
echo "=== DEPLOYMENT STARTED: $(date) ===" > "$LOG"

printf '1\n2\ny\n' | python3 -u create-competition.py 2>&1 | tee -a "$LOG"
EXIT_CODE=${PIPESTATUS[0]}

echo "=== DEPLOYMENT EXIT CODE: $EXIT_CODE ===" >> "$LOG"
echo "=== DEPLOYMENT ENDED: $(date) ===" >> "$LOG"
exit "$EXIT_CODE"
