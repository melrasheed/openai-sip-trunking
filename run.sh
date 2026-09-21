#!/usr/bin/env bash
# Creates the virtual environment, installs requirements, and starts the
# contextualised banking voice agent. Safe to re-run.
#
#   ./run.sh              start on PORT from .env (default 8000)
#   PORT=8100 ./run.sh    start on a different port
#   ./run.sh --seed       reseed the demo customers before starting
#   ./run.sh --reinstall  recreate the virtual environment
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"
VENV_PYTHON="$VENV_DIR/bin/python"
STAMP="$VENV_DIR/.requirements.sha256"

SEED=0
for arg in "$@"; do
  case "$arg" in
    --reinstall) [[ -d "$VENV_DIR" ]] && { echo "Removing existing virtual environment..."; rm -rf "$VENV_DIR"; } ;;
    --seed) SEED=1 ;;
  esac
done

if [[ ! -x "$VENV_PYTHON" ]]; then
  PYTHON=""
  for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
  done
  if [[ -z "$PYTHON" ]]; then
    echo "No Python interpreter found. Install Python 3.10 or later." >&2
    exit 1
  fi
  echo "Creating virtual environment using $($PYTHON -c 'import sys; print(sys.executable)')"
  "$PYTHON" -m venv "$VENV_DIR"
fi

# Reinstall only when requirements.txt has actually changed.
if command -v sha256sum >/dev/null 2>&1; then
  HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
else
  HASH="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
fi
INSTALLED="$(cat "$STAMP" 2>/dev/null || true)"

if [[ "$HASH" != "$INSTALLED" ]]; then
  echo "Installing dependencies..."
  "$VENV_PYTHON" -m pip install --upgrade pip --quiet --disable-pip-version-check
  "$VENV_PYTHON" -m pip install -r requirements.txt --quiet --disable-pip-version-check
  echo "$HASH" > "$STAMP"
else
  echo "Dependencies already up to date."
fi

if [[ ! -f .env ]]; then
  echo "WARNING: no .env found. Copy .env.example to .env and fill in your Azure OpenAI values." >&2
fi

if [[ "$SEED" == "1" ]]; then
  echo "Reseeding the demo customer data..."
  "$VENV_PYTHON" -c "import sys; sys.path.insert(0, 'src'); import db; print('Seeded', db.seed(force=True), 'customers')"
fi

echo "Starting the agent (Ctrl+C to stop). UI: http://127.0.0.1:${PORT:-8000}/"
exec "$VENV_PYTHON" -u src/app.py