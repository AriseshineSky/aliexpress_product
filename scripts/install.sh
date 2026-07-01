#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> aliexpress_product installer (Linux/macOS)"
echo "Project: $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 not found. Install Python 3.10+ first." >&2
  exit 1
fi

PY_MINOR="$(python3 -c 'import sys; print(sys.version_info.minor)')"
if (( PY_MINOR < 10 )); then
  echo "Python 3.10+ required, found 3.${PY_MINOR}" >&2
  exit 1
fi

python3 -c 'import sys; print("Python:", sys.version.split()[0], sys.executable)'

if [[ ! -d .venv ]]; then
  echo "[1/4] Creating virtual environment .venv"
  python3 -m venv .venv
fi

VENV_PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Virtual environment creation failed: $VENV_PYTHON not found" >&2
  exit 1
fi

echo "[2/4] Installing Python dependencies"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r requirements.txt

echo "[3/4] Installing Playwright Chromium"
"$VENV_PYTHON" -m playwright install chromium

echo "[4/4] Checking .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit it with your credentials before running."
else
  echo ".env already exists (not overwritten)."
fi

echo
echo "Install complete. Next steps:"
echo "  1. Edit .env with ES / Webshare / API credentials"
echo "  2. Run: ./start.sh"
