# Twitch Auto-Recorder

Local multi-streamer Twitch recorder (**HTML UI** + **Python streamlink helper**) with seamless segment joins, Music only (Demucs), Auto Music only, and Convert upload.

**Current version:** 6.26 — see badge in `twitch-auto-recorder.html` / `HELPER_VERSION` in `twitch-recorder-server.py` (also `VERSION`).

## Open the app (any browser)

**Primary URL (GitHub Pages):**  
https://ewanders1-web.github.io/twitch-auto-recorder/

The Pages UI works on **any computer**. Recording still requires the **Python helper on the machine that should save files** (streamlink → `~/TwitchRecordings`). GitHub Pages cannot record by itself.

The helper listens on `127.0.0.1:8765` only and sends `Access-Control-Allow-Origin: *`, so the Pages UI can talk to the helper when both run on the **same** machine.

## Install the helper (Mac / Linux)

One-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/install.sh | bash
```

Optional: start the helper in the background after install:

```bash
curl -fsSL https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/install.sh | bash -s -- --start
```

Install locations:

- **macOS:** `~/Library/Application Support/TwitchRecorder`
- **Linux:** `~/.local/share/TwitchRecorder`

Then:

```bash
pip install streamlink
# optional Music only / seamless:
# pip install demucs   # + ffmpeg on PATH
cd "$HOME/Library/Application Support/TwitchRecorder"   # or ~/.local/share/TwitchRecorder on Linux
python3 twitch-recorder-server.py
```

## Windows (bonus)

```powershell
irm https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/install.ps1 | iex
# or download install.ps1 and: .\install.ps1 -Start
```

Installs to `%LOCALAPPDATA%\TwitchRecorder`. Then `pip install streamlink` and run `python twitch-recorder-server.py` from that folder.

## Quick start (manual)

1. Copy `twitch-recorder-server.py` and `twitch-auto-recorder.html` into the install dir (or keep them next to each other).
2. Run the helper: `python3 twitch-recorder-server.py`.
3. Open **https://ewanders1-web.github.io/twitch-auto-recorder/** (or `http://127.0.0.1:8765/` if the helper serves the HTML).
4. Optional Music only: `python3 -m pip install demucs` (needs `ffmpeg`).

Details: see `README-recorder.txt`.

## Auto-updates

The helper can check this public repo for a newer `VERSION` and pull `twitch-auto-recorder.html`, `twitch-recorder-server.py`, and `README-recorder.txt`. Use **Check for updates** in the UI (v6.25+).

## Privacy

Twitch Client ID/Secret stay in browser localStorage only — they are never committed here.

## Security note

The helper binds **localhost only** (`127.0.0.1`). Do not bind `0.0.0.0`.
