#!/bin/bash
cd /home/hna/dev/dawgsec/tezcatlipoca-fix
LOG="/home/hna/dev/dawgsec/tezcatlipoca-fix/deploy.log"
echo "=== DEPLOYMENT STARTED: $(date) ===" > "$LOG"
printf '1\n2\ny\n' | python3 create-competition.py competitions/cde-2026 2>&1 | tee -a "$LOG"
EXIT_CODE=${PIPESTATUS[0]}
echo "=== DEPLOYMENT EXIT CODE: $EXIT_CODE ===" >> "$LOG"
echo "=== DEPLOYMENT ENDED: $(date) ===" >> "$LOG"
exit $EXIT_CODE
