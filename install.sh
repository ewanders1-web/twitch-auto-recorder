#!/usr/bin/env bash
# Twitch Auto-Recorder — portable install (macOS / Linux)
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --start
set -euo pipefail

REPO="ewanders1-web/twitch-auto-recorder"
BRANCH="main"
RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
FILES=(
  "twitch-auto-recorder.html"
  "twitch-recorder-server.py"
  "README-recorder.txt"
  "VERSION"
)

START=0
for arg in "$@"; do
  case "$arg" in
    --start|-s) START=1 ;;
    -h|--help)
      echo "Usage: install.sh [--start]"
      echo "  Installs Twitch Auto-Recorder helper into a user data dir."
      echo "  --start  Launch the helper in the background after install."
      exit 0
      ;;
  esac
done

os="$(uname -s 2>/dev/null || echo unknown)"
case "$os" in
  Darwin)
    INSTALL_DIR="${HOME}/Library/Application Support/TwitchRecorder"
    ;;
  Linux|*)
    INSTALL_DIR="${HOME}/.local/share/TwitchRecorder"
    ;;
esac

mkdir -p "$INSTALL_DIR"
echo "Installing Twitch Auto-Recorder into:"
echo "  $INSTALL_DIR"
echo

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for f in "${FILES[@]}"; do
  echo "  downloading $f …"
  curl -fsSL "${RAW}/${f}" -o "${tmp}/${f}"
done

# Atomic-ish replace
for f in "${FILES[@]}"; do
  mv -f "${tmp}/${f}" "${INSTALL_DIR}/${f}"
done

# Keep index.html in sync for local helper convenience (optional)
cp -f "${INSTALL_DIR}/twitch-auto-recorder.html" "${INSTALL_DIR}/index.html" 2>/dev/null || true

ver="unknown"
if [[ -f "${INSTALL_DIR}/VERSION" ]]; then
  ver="$(tr -d '[:space:]' < "${INSTALL_DIR}/VERSION")"
fi
echo
echo "Installed version: ${ver}"
echo
echo "Next steps:"
echo "  1) pip install streamlink"
echo "     (optional Music only / seamless: pip install demucs  +  ffmpeg on PATH)"
echo "  2) Start the helper:"
echo "       cd \"${INSTALL_DIR}\""
echo "       python3 twitch-recorder-server.py"
echo "     Or:  \"$0\" --start"
echo "  3) Open the UI (any machine):"
echo "       https://ewanders1-web.github.io/twitch-auto-recorder/"
echo "     Or locally while helper runs: http://127.0.0.1:8765/"
echo
echo "Recordings save to ~/TwitchRecordings on this machine."
echo "Helper listens on 127.0.0.1:8765 only (Pages → localhost is OK via CORS)."
echo

if [[ "$START" -eq 1 ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found — install Python 3, then re-run with --start." >&2
    exit 1
  fi
  log="${INSTALL_DIR}/helper.log"
  # Prefer nohup so closing the terminal does not kill the helper
  (
    cd "$INSTALL_DIR"
    nohup python3 twitch-recorder-server.py >>"$log" 2>&1 &
    echo $! > "${INSTALL_DIR}/helper.pid"
  )
  echo "Helper started in background (pid $(cat "${INSTALL_DIR}/helper.pid"))."
  echo "Log: $log"
  echo "Health: http://127.0.0.1:8765/api/health"
fi
