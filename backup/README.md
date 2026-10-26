# Twitch Backup Recorder

A separate Mac recorder based on the workflow in your supplied recorder. It uses its own recording process and folders, so it can run alongside your existing helper without sharing port 8765 or changing that helper.

## Start

1. Keep this folder together in a location with plenty of free space.
2. Double-click **Install.command** once. This installs Python 3.11 and FFmpeg through Homebrew and Streamlink, Demucs and PyTorch into this folder's private environment. Homebrew must already be installed (https://brew.sh).
3. Double-click **Start Backup.command** to open the visual control panel. Add streamers and click **Start watching**. The default streamer is `whistleface`.
4. Leave the window open. It checks every 30 seconds, records when the channel is live, and checks again after recording ends. Use **Stop watching** in the interface to stop capture and queue processing.

The first vocal-removal job downloads Demucs model weights. Vocal separation can take longer than the recording itself and may leave some vocal remnants or remove some instruments. No center-cancellation substitute is silently used.

## Output

Each capture has a unique folder inside **Recordings**:

- `original.ts`: retained original stream audio (or original stream with video if an audio-only rendition is unavailable).
- `music-parts/CHANNEL-TIMESTAMP-music-part1of8.ogg`, `part2of8.ogg`, etc.: instrumental segments. Each is approximately 30 minutes, within audio-packet precision; the last can be shorter. The total comes from the files actually produced.
- `music-parts/manifest.json`: part numbers, total and measured durations.
- `status.json`: recording/processing state and any error.
- `recording.log`, `processing.log`, `worker.log`: troubleshooting details.
- `audio.wav` and `separated/`: retained intermediate audio and separated stems.

Parts appear together only after all segments have been encoded and checked. Recording resumes independently while previous sessions process. Vocal removal is queued one job at a time. Reconnects that end a capture create separate sessions with their own numbering; this version does not merge interrupted sessions or upload to Drive.

## Resume failed or interrupted processing

From Terminal in this folder:

```sh
export PATH="$PWD/.venv/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
.venv/bin/python recorder.py process "Recordings/SESSION-FOLDER"
```

Completed sessions are skipped. Originals are never deleted. If the watcher was forcibly closed, its channel `.lock` file may remain inside Recordings. Check that no other backup watcher is running before removing that lock and restarting.

Keep the Mac powered while background processing runs. The launchers prevent idle sleep during watching and processing. Internet or Twitch failures can still cause gaps; running both recorders on one computer does not protect against that computer losing power or network access. Recording begins when you launch this tool; there is no automatic login/startup service.

## Validation

The segment/export pipeline was tested with generated audio, including multiple numbered parts, final short part, duration checks, and repeat-processing protection. The interface was checked in a browser: adding/removing streamers, tab navigation and dependency display. Local request authentication and file path restrictions passed checks. Live Twitch capture and Demucs separation require the installer and were not run during preparation.

## GitHub Pages

The included workflow publishes only index.html from the main branch. Set the repository Pages source to GitHub Actions. The local helper allows the interface at https://ewanders1-web.github.io; change PAGE_ORIGIN in interface.py if using another GitHub account. Run Start Backup.command locally even when visiting the hosted interface. Allow local network access if the browser requests it. Recordings and local settings are excluded from Git.
