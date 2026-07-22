#!/usr/bin/env bash
# Install / uninstall cron job: seed Redis queue every N hours (default 4).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEED_SH="$ROOT/scripts/seed_redis_scheduled.sh"
# Hours between runs; override with SEED_CRON_HOURS=2 ./scripts/install_seed_cron.sh install
HOURS="${SEED_CRON_HOURS:-4}"
MARKER="# aliexpress_product seed_redis every ${HOURS}h"
# Also match older 2h marker so reinstall cleans it up
OLD_MARKERS=(
  "# aliexpress_product seed_redis every 2h"
  "# aliexpress_product seed_redis every 4h"
  "$MARKER"
)
CRON_LINE="0 */${HOURS} * * * $SEED_SH $MARKER"

usage() {
  cat <<EOF
Usage: SEED_CRON_HOURS=4 $(basename "$0") [install|uninstall|status|run-once]

  install     Install crontab entry (default every 4 hours at :00)
  uninstall   Remove this project's crontab entry
  status      Show whether the cron entry is installed
  run-once    Run one seed now (same as the scheduled job)
EOF
}

ensure_executable() {
  chmod +x "$SEED_SH"
  chmod +x "$0"
}

# Drop any historical/current project seed cron lines from crontab text.
filter_seed_lines() {
  local text="$1"
  local m
  for m in "${OLD_MARKERS[@]}"; do
    text="$(printf '%s\n' "$text" | grep -vF "$m" || true)"
  done
  # Catch any variant: ... seed_redis every Nh
  text="$(printf '%s\n' "$text" | grep -vF 'aliexpress_product seed_redis every' || true)"
  printf '%s\n' "$text"
}

cmd="${1:-install}"

case "$cmd" in
  install)
    ensure_executable
    existing="$(crontab -l 2>/dev/null || true)"
    filtered="$(filter_seed_lines "$existing")"
    {
      printf '%s\n' "$filtered"
      printf '%s\n' "$CRON_LINE"
    } | grep -v '^$' | crontab -
    echo "Installed cron:"
    echo "  $CRON_LINE"
    echo "Logs: $ROOT/logs/seed_redis_scheduled.log"
    echo "Check: crontab -l"
    ;;
  uninstall)
    existing="$(crontab -l 2>/dev/null || true)"
    filtered="$(filter_seed_lines "$existing")"
    if [[ -z "${filtered//[[:space:]]/}" ]]; then
      crontab -r 2>/dev/null || true
    else
      printf '%s\n' "$filtered" | grep -v '^$' | crontab -
    fi
    echo "Removed cron entries matching aliexpress_product seed_redis"
    ;;
  status)
    if crontab -l 2>/dev/null | grep -F 'aliexpress_product seed_redis every' >/dev/null; then
      echo "INSTALLED:"
      crontab -l | grep -F 'aliexpress_product seed_redis every'
    else
      echo "NOT installed"
    fi
    ;;
  run-once)
    ensure_executable
    exec "$SEED_SH"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 1
    ;;
esac
