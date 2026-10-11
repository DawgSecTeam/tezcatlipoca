#!/usr/bin/env bash
# Run 6 — validation scrim after the 2026-10-08 fixes. Executes what is queued
# behind the range reset, so the follow-up turn is one command.
#
#   1. instrumented 60-min automated scrim (the new code paths, live)
#   2. the run-dir evidence check (run6-verify.py)
#   3. the human-path rehearsal: plant_assume_breach (deploy-time seed + tz-ready)
#
# Run 6 is 60 rather than 90 minutes: every unknown here (Windows realm channels,
# seed survival across the director start, defrun/hide, randomised re-plants)
# resolves in the first blue cycles, and the reset + rehearsal dominate the clock.
set -uo pipefail
cd "$(dirname "$0")/.."                       # tezcatlipoca/
export PATH="$HOME/.hermes/cache/scratch/scrim-shim:$PATH"

TEZ="$PWD"
RUN_LOG="$TEZ/logs/scrim-persist-20261008-run6.log"

step() { echo; echo "===== $* ====="; }

step "1/3 run 6 — automated scrim (60 min, new code paths)"
.venv/bin/python -u run-agent-scrim.py \
  --competition scrim-one --teams 1 --duration-min 60 --seed-depth 3 \
  --blue-watchdog --keep-range --skip-deploy \
  --llm-base-url http://100.64.0.19:8000/v1 --red-model qwen3.8-flash-next \
  --blue-base-url http://127.0.0.1:8080/v1 --blue-model qwen3.8-27b \
  --reasoning-effort '' --red-ip 10.0.0.198 --red-gw 10.0.0.1 --red-storage hdd \
  > "$RUN_LOG" 2>&1
echo "run 6 exit=$? (log: $RUN_LOG)"

step "2/3 verify the run's own pulled state"
RUN_DIR=$(ls -td "$TEZ"/competitions/scrim-one/.automated-tests/run-* | head -1)
echo "run dir: $RUN_DIR"
/usr/bin/python3 "$TEZ/competitions/scrim-one/run6-verify.py" "$RUN_DIR"

step "3/3 human-path rehearsal — deploy-time assume-breach seed"
.venv/bin/python -u competitions/scrim-one/rehearse-assume-breach.py
