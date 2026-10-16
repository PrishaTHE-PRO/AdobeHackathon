#!/usr/bin/env bash
# One-shot setup: creates a virtualenv and installs every dependency.
# Usage:  ./setup.sh        (then: source .venv/bin/activate)
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  echo "Python 3.10+ is required (found: $("$PY" --version))"; exit 1
fi

[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Done. Activate the environment with:  source .venv/bin/activate"
echo "Then run the engine with:             python -m engine.main"
