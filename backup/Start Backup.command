#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  cd "$HOME/Library/Application Support/TwitchBackupRecorder"
fi
export PATH="$PWD/.venv/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ ! -x .venv/bin/python ]; then
  echo 'Run Install.command first.'
  exit 1
fi
exec caffeinate -i .venv/bin/python interface.py
