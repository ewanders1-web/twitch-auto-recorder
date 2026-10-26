#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
echo 'Installing backup recorder dependencies into a private environment.'
if ! command -v brew >/dev/null; then
  echo 'Install Homebrew from https://brew.sh first, then run this installer again.'
  read -r -p 'Press Return to close.'
  exit 1
fi
brew install python@3.11 ffmpeg
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install 'numpy<2' 'torch==2.5.1' 'torchaudio==2.5.1' 'demucs==4.0.1' streamlink
export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/python recorder.py check
echo 'Ready. Open Start Backup.command to record.'
read -r -p 'Press Return to close.'
