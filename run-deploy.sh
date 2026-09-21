#!/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

LOG="$REPO_DIR/deploy.log"
echo "=== DEPLOYMENT STARTED: $(date) ===" > "$LOG"

set +e
printf '1\n2\ny\n' | python3 -u create-competition.py 2>&1 | tee -a "$LOG"
EXIT_CODE=${PIPESTATUS[1]}
set -e

echo "=== DEPLOYMENT EXIT CODE: $EXIT_CODE ===" >> "$LOG"
echo "=== DEPLOYMENT ENDED: $(date) ===" >> "$LOG"
exit "$EXIT_CODE"
