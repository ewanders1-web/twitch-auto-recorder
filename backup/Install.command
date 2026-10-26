#!/bin/bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
trap 'echo "Installation failed. Read the error above, then run the installer again."' ERR
if ! command -v brew >/dev/null; then
  echo 'Install Homebrew from https://brew.sh first, then run this installer again.'
  exit 1
fi
brew install python@3.11 ffmpeg
INSTALL_DIR="$HOME/Library/Application Support/TwitchBackupRecorder"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
SOURCE='https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/backup'
# Download code only. Never replace settings, recordings, or the private environment.
for file in recorder.py interface.py index.html requirements.txt README.md 'Start Backup.command'; do
  encoded="${file// /%20}"
  curl --fail --location --retry 3 "$SOURCE/$encoded" -o "$file.download"
  mv "$file.download" "$file"
done
chmod +x 'Start Backup.command'
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/python recorder.py check
echo "Ready. Starting the recorder. Your files are in $INSTALL_DIR/Recordings"
exec bash 'Start Backup.command'
