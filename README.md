# Twitch Auto-Recorder

Local multi-streamer Twitch recorder (**HTML UI** + **Python streamlink helper**) with seamless segment joins, Music only (Demucs), Auto Music only, and Convert upload.

**Current version:** 6.44 — after-stream voice removal / split / Drive send survives a helper restart: queued or running stream jobs are saved in `.drive-pipeline.json` and re-queued ~20 s after the helper starts again (crash, reboot, power blip, manual restart), up to 3 tries and for up to 3 days. v6.43: the helper updates itself unattended: it checks GitHub ~2 min after start and every ~3 h, and installs + restarts only when nothing is recording (incl. a stream-restart wait), removing voice, splitting, joining or sending to Drive, after a few idle minutes (Settings → "Install helper updates automatically when idle", default on; `TWITCH_RECORDER_AUTO_UPDATE=0` to disable). Downloads are validated (compile + version match + `--self-test`) and the old files are backed up to `.update-backup/` before the swap; state shows in the update panel, `/api/health.autoUpdate` and the Drive status file. Helpers on 6.42 or older need one last **Check for updates → Install update**. v6.42: when a streamer ends and immediately restarts their broadcast, the helper keeps retrying for 8 min (backing off to every 30 s) while they are Auto-Rec armed, and stitches the new broadcast onto the same session instead of giving up after ~1 min (`TWITCH_RECORDER_RESTART_GRACE_SECS`, default 480). v6.41: each helper writes a status file for its computer into the Drive target (`Recorder status/<computer>.txt` + `.json`: up/armed/recording/Drive sends/errors, refreshed every 5 min and on every change) so all recording computers can be checked from anywhere; a file older than 15 min means that computer is off, asleep or its helper is down. v6.40 keeps the computer awake (no idle/system sleep; screen can still turn off) while recording, removing voice, splitting or sending to Drive, plus ~10 min after; optional "while Auto-Rec is armed" hold (Settings). v6.39 sweeps stale Drive `.{name}.partial-*` temps under the Auto-send delivery target and its `Originals/` folder (startup + each delivery tick; age ~1h, or ~5 min when idle). v6.38 sends the original recording to Drive `Originals` first (kept locally), then voice-remove + split parts under 50 MB; no-clobber across computers and Windows Drive detection. v6.37 clears local parts after a successful send; v6.36 added the after-stream pipeline. v6.35 added Convert → Google Drive → remove voice. v6.31 optional **Unmute add-on** (`extension/`). Helper recordings and Music only exports are **OGG**. See badge / `HELPER_VERSION` / `VERSION`.

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

The helper can check this public repo for a newer `VERSION` and pull `twitch-auto-recorder.html`, `twitch-recorder-server.py`, and `README-recorder.txt`. Use **Check for updates** in the UI (v6.25+). From v6.43 the helper also does this by itself when idle (every ~3 h; Settings → "Install helper updates automatically when idle"); the previous files are kept in `.update-backup/` in the install dir.

## Unmute add-on (v6.31, optional)

Chrome MV3 extension in `extension/` (installed as `twitch-unmute-extension`). When you click **Arm**, it unmutes the open `twitch.tv/<username>` tab (Chrome tab mute + Twitch player mute/volume).

1. Get the folder (`install.sh` puts it in the install dir as `twitch-unmute-extension`, or use `extension/`).
2. Chrome → `chrome://extensions`
3. Enable **Developer mode**
4. **Load unpacked** → pick the `twitch-unmute-extension` folder
5. Reload the recorder page.

## Privacy

Twitch Client ID/Secret stay in browser localStorage only — they are never committed here.

## Security note

The helper binds **localhost only** (`127.0.0.1`). Do not bind `0.0.0.0`.
