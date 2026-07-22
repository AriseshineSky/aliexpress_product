#!/usr/bin/env bash
# Every-N-hours Redis queue seeder:
#   1) rebuild/clear alixq3:urls
#   2) enqueue not-yet-crawled URLs (strict priority first, then rating/reviews/sold)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/seed_redis_scheduled.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

VENV_PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "[$STAMP] ERROR: .venv missing at $VENV_PYTHON" | tee -a "$LOG_FILE"
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  echo "[$STAMP] ERROR: .env missing" | tee -a "$LOG_FILE"
  exit 1
fi

{
  echo "========== $STAMP seed start =========="
  echo "cwd=$ROOT"
  # Default seed_redis_priority.py already:
  #   - deletes alixq3:urls (clears queue) unless --append
  #   - clears stale claims
  #   - phase1 strict priority, then phase2 rating→reviews→sold
  #   - skips URLs already in products index
  "$VENV_PYTHON" "$ROOT/scripts/seed_redis_priority.py"
  echo "========== $(date '+%Y-%m-%d %H:%M:%S') seed done =========="
  echo
} >>"$LOG_FILE" 2>&1
