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

echo "==> Starting alixq3.py"
echo "Python: $VENV_PYTHON"
echo "Working dir: $ROOT"
echo

exec "$VENV_PYTHON" alixq3.py
