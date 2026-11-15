#!/bin/zsh
set -e
cd "${0:A:h}"
PYTHON_BIN="${IEDA_PYTHON:-$PWD/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Run SETUP.command in this project first."
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
"$PYTHON_BIN" start_webapp.py
