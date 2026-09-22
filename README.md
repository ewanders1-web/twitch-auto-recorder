# Twitch Auto-Recorder

Local multi-streamer Twitch recorder (HTML UI + Python streamlink helper) with seamless segment joins, Music only (Demucs), Auto Music only, and Convert upload.

**Current version:** see badge in `twitch-auto-recorder.html` / `HELPER_VERSION` in `twitch-recorder-server.py` (also `VERSION`).

## Quick start (Mac)

1. Copy `twitch-recorder-server.py` and `twitch-auto-recorder.html` into `~/Library/Application Support/TwitchRecorder/` (or open the HTML via your Desktop launcher).
2. Run the helper: `python3 twitch-recorder-server.py` (or use the LaunchAgent / Desktop app).
3. Open the HTML (or `http://127.0.0.1:8765/` if the helper serves it).
4. Optional Music only: `python3 -m pip install demucs` (needs `ffmpeg`).

Details: see `README-recorder.txt`.

## Auto-updates

The helper can check this public repo for a newer `VERSION` and pull `twitch-auto-recorder.html`, `twitch-recorder-server.py`, and `README-recorder.txt`. Use **Check for updates** in the UI (v6.25+).

## Privacy

Twitch Client ID/Secret stay in browser localStorage only — they are never committed here.
