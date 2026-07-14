#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV_PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  cat <<'EOF' >&2
.venv not found.

Run the installer first:
  ./install.sh
EOF
  exit 1
fi

if [[ ! -f "$ROOT/.env" ]]; then
  cat <<'EOF' >&2
.env not found.

Copy .env.example to .env and fill in credentials:
  cp .env.example .env
EOF
  exit 1
fi

export PYTHONUNBUFFERED=1

# Read PROXY_MODE from .env without sourcing the whole file (passwords may have special chars).
PROXY_MODE="$(
  "$VENV_PYTHON" - <<'PY'
from pathlib import Path
try:
    from dotenv import dotenv_values
    vals = dotenv_values(Path(".env"))
except Exception:
    vals = {}
mode = (vals.get("PROXY_MODE") or "rotate").strip().lower()
print(mode)
PY
)"

echo "==> PROXY_MODE=${PROXY_MODE}"
echo "Python: $VENV_PYTHON"
echo "Working dir: $ROOT"
echo

if [[ "$PROXY_MODE" == "pool" ]]; then
  echo "==> Starting scripts/run_fixed_pool.py (homepage warmup + proxy pool)"
  exec "$VENV_PYTHON" scripts/run_fixed_pool.py
fi

echo "==> Starting alixq3.py"
exec "$VENV_PYTHON" alixq3.py
