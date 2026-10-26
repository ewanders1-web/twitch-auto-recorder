#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/.venv/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
PYTHON=python3
if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; fi
if command -v caffeinate >/dev/null; then
  caffeinate -i "$PYTHON" interface.py
else
  "$PYTHON" interface.py
fi
