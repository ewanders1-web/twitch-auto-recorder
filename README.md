# Twitch Auto-Recorder

Local multi-streamer Twitch recorder (**HTML UI** + **Python streamlink helper**) with seamless segment joins, Music only (Demucs), Auto Music only, and Convert upload.

**Current version:** 6.77 — an original recording whose local file was deleted or moved (outside the page) no longer retries Drive/Originals forever: after 3 checks over 30+ min (and only while ~/TwitchRecordings is mounted and not empty) it shows "Local file gone — not sent", stops counting as failed, and offers Check again / Dismiss; the helper re-checks every 6 h and sends it if the file comes back (`TWITCH_RECORDER_MARK_GONE_ORIGINALS=0` for the old behaviour). 6.76 — helpers with auto-update off that are older than v6.70 (they never check GitHub on their own) now get the same sticky Install update banner: the GitHub Pages copy of the page knows it is the latest release, and the page asks the helper to check GitHub every ~3 h while auto-update is off; never installs by itself (reload the recorder tab to get it). 6.75 — the computer name stays the same when the network changes (a Mac's hostname could flip, e.g. Silver Bullet as both "Erics-14-Silver-Bullet" and "mac"): saved once in `~/TwitchRecordings/.computer-identity.json`, older names kept as aliases so their Drive sends count as this computer's and their stale status files are removed (`TWITCH_RECORDER_COMPUTER_NAME` still overrides). 6.74 — one copy per stream knows about voice-removal quality: a Fast set from a computer without Demucs no longer makes the Demucs computers skip their All voices parts; the Fast computer holds its parts (up to 3 h) while a live Demucs computer works on the same stream, and an All voices set replaces same-named Fast parts in Drive (`TWITCH_RECORDER_QUALITY_ONE_COPY=0` to turn off, `TWITCH_RECORDER_FAST_DEFER_SECS` for the hold). 6.73 — louder, even volume (two-pass loudnorm to -14 LUFS / -1.5 dBTP on every music-only file; `TWITCH_RECORDER_LOUDNORM=0` to turn off) and better music encodes (Opus 48 kHz 180 kb/s CBR, Vorbis fallback; `TWITCH_RECORDER_MUSIC_CODEC=vorbis` for the old encode); a 4 h set is 7 equal parts of ~34 min under 50 MB, cut from exact intervals so there is no tiny padding-only last part. 6.72 — voice removal is back to **All voices** (full Demucs wipe) by default for every streamer including whistleface (Auto = All voices with Demucs, else Fast); Talk only stays selectable and only installs speechbrain when picked; a saved Talk only from 6.69–6.71 resets to Auto once. 6.71 — one copy per stream for originals too: the recording computers take turns (about 2.5 min apart) and skip copying an original into Drive/Originals when another computer's copy that covers the whole stream is already there (`TWITCH_RECORDER_ONE_COPY_ORIGINALS=0` to turn off). 6.70 — when automatic helper updates are off, the helper still checks GitHub on the normal cadence and surfaces a newer version (status file + sticky page banner with Install update); it never auto-installs while auto-update is off. 6.69 — Talk only hybrid voice removal was the default (until 6.72) (remove DJ talk, keep song vocals) for every streamer including whistleface; All voices = old Better full wipe; Fast unchanged; voiceprints in `~/TwitchRecordings/.voiceprints`; `TWITCH_RECORDER_TALKONLY_CUTOFF` / `TWITCH_RECORDER_TALKONLY_AUTOINSTALL=0`. 6.68 — on a Mac, Drive sends that failed with "Resource deadlock avoided" (an online-only Google Drive folder) now ask macOS to download that folder and retry, with a plain-English fix if it still fails (`TWITCH_RECORDER_MAC_MATERIALIZE=0` to leave the macOS setting alone). 6.67 — voice-removed Drive parts are named like `whistleface-2026-11-03-Tue-part 1 of 4.ogg` (a second broadcast the same day gets ` (2)`; `TWITCH_RECORDER_PART_NAMES=classic` for the old names). 6.66 — the helper sets up a missing streamlink / ffmpeg by itself when idle (no admin; ffmpeg is a checksum-verified portable build on Windows/Linux; `TWITCH_RECORDER_PROVISION_TOOLS=0` to turn off) — full changelog in `README-recorder.txt`. Earlier: 6.46 — stream restart grace survives a closed/crashed recorder page (stale heartbeat ≈2 min → helper keeps the v6.42 8-minute retry window instead of giving up from a stale empty Auto-Rec list). v6.45 — voice removal quality: Convert → **Fast** (ffmpeg center-cancel, no demucs needed; merged from Twitchy Too) or **Better** (Demucs), default Auto = Better when demucs is installed, else Fast. Applies to History/Auto Music only, Convert upload, Drive → remove voice, Drive links and the after-stream remove voice → Drive pipeline; same one-at-a-time queue, same `-music.ogg` names and <50 MB Drive parts. v6.44: after-stream voice removal / split / Drive send survives a helper restart: queued or running stream jobs are saved in `.drive-pipeline.json` and re-queued ~20 s after the helper starts again (crash, reboot, power blip, manual restart), up to 3 tries and for up to 3 days. v6.43: the helper updates itself unattended: it checks GitHub ~2 min after start and every ~3 h, and installs + restarts only when nothing is recording (incl. a stream-restart wait), removing voice, splitting, joining or sending to Drive, after a few idle minutes (Settings → "Install helper updates automatically when idle", default on; `TWITCH_RECORDER_AUTO_UPDATE=0` to disable). Downloads are validated (compile + version match + `--self-test`) and the old files are backed up to `.update-backup/` before the swap; state shows in the update panel, `/api/health.autoUpdate` and the Drive status file. Helpers on 6.42 or older need one last **Check for updates → Install update**. v6.42: when a streamer ends and immediately restarts their broadcast, the helper keeps retrying for 8 min (backing off to every 30 s) while they are Auto-Rec armed, and stitches the new broadcast onto the same session instead of giving up after ~1 min (`TWITCH_RECORDER_RESTART_GRACE_SECS`, default 480). v6.41: each helper writes a status file for its computer into the Drive target (`Recorder status/<computer>.txt` + `.json`: up/armed/recording/Drive sends/errors, refreshed every 5 min and on every change) so all recording computers can be checked from anywhere; a file older than 15 min means that computer is off, asleep or its helper is down. v6.40 keeps the computer awake (no idle/system sleep; screen can still turn off) while recording, removing voice, splitting or sending to Drive, plus ~10 min after; optional "while Auto-Rec is armed" hold (Settings). v6.39 sweeps stale Drive `.{name}.partial-*` temps under the Auto-send delivery target and its `Originals/` folder (startup + each delivery tick; age ~1h, or ~5 min when idle). v6.38 sends the original recording to Drive `Originals` first (kept locally), then voice-remove + split parts under 50 MB; no-clobber across computers and Windows Drive detection. v6.37 clears local parts after a successful send; v6.36 added the after-stream pipeline. v6.35 added Convert → Google Drive → remove voice. v6.31 optional **Unmute add-on** (`extension/`). Helper recordings and Music only exports are **OGG**. See badge / `HELPER_VERSION` / `VERSION`.

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

The helper can check this public repo for a newer `VERSION` and pull `twitch-auto-recorder.html`, `twitch-recorder-server.py`, and `README-recorder.txt`. Use **Check for updates** in the UI (v6.25+). From v6.43 the helper also does this by itself when idle (every ~3 h; Settings → "Install helper updates automatically when idle"); the previous files are kept in `.update-backup/` in the install dir. From v6.70, with auto-update off the helper still checks GitHub and shows that a newer version is available (status file + page banner) — it never installs until you click **Install update**.

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


## Independent backup recorder (Mac and Windows)

Install a second recorder on each computer with its own recordings and settings. [Shareable setup page](https://ewanders1-web.github.io/twitch-auto-recorder/backup/guide.html) · [Source and instructions](backup/README.md).
