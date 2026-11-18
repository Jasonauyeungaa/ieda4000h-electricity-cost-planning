#!/bin/zsh
set -e
cd "${0:A:h}"
PYTHON_BIN="${IEDA_PYTHON:-$PWD/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Run SETUP.command in this project first."
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("cvxpy") else 1)'; then
  echo "Run SETUP.command to install the CVXPY dependencies for this version."
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
"$PYTHON_BIN" start_webapp.py
