# Twitch Backup Recorder — Mac and Windows

**Share this setup page:** https://ewanders1-web.github.io/twitch-auto-recorder/backup/guide.html

Install on every computer that should record. Computers work independently: each has its own streamers, originals, settings and processing queue. The backup can run alongside the main recorder; its local control panel uses port 8766.

## Mac

Install Homebrew from https://brew.sh first. Paste into Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/backup/Install.command -o /tmp/twitch-backup-install.command && bash /tmp/twitch-backup-install.command
```

The installer installs Python 3.11 and FFmpeg with Homebrew, creates a private Python environment, installs recording/vocal separation tools, and starts the control panel.

Next time, open `~/Library/Application Support/TwitchBackupRecorder/Start Backup.command` in Finder. A Mac supported by current Homebrew and Python 3.11 is required.

## Windows 10 / 11, x64 (Intel / AMD)

Paste into Windows PowerShell:

```powershell
Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/backup/Install.ps1' -OutFile "$env:TEMP\twitch-backup-install.ps1"; if ($?) { powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\twitch-backup-install.ps1" }
```

The installer downloads the recorder into `%LOCALAPPDATA%\TwitchBackupRecorder`, installs Python 3.11 and FFmpeg through winget if needed, creates a private environment, checks the tools and starts the panel. Accept installer prompts. If winget is missing, install Microsoft App Installer or manually install Python 3.11 (with the Python launcher) and FFmpeg/ffprobe on PATH, then retry.

Next time, open `%LOCALAPPDATA%\TwitchBackupRecorder\Start Backup.cmd`. Windows ARM is not currently supported by these pinned dependencies.

Alternatively, download the repository ZIP from GitHub, extract it, open `backup`, and double-click `Install.cmd`. Windows needs both `Install.cmd` and `Install.ps1` together.

## Use on each computer

1. Leave the launcher window open and visit http://127.0.0.1:8766/ in that computer's browser.
2. Add streamers and click **Start watching**. Capture begins when a watched channel is live; it checks about every 30 seconds.
3. **Stop watching** ends capture and queues vocal removal. Allow up to 45 seconds if a channel availability check is in progress.
4. Use **Recordings** to download originals and numbered music parts. Let processing finish before shutting down.

Closing the browser tab keeps watching active. Use Stop watching before closing the launcher. Keep the computer powered and awake; Mac launchers prevent idle sleep and the Windows control panel prevents idle system sleep while it is running. No automatic login/startup service is installed. Running both recorders on one computer does not protect against power or network loss.

The hosted panel at https://ewanders1-web.github.io/twitch-auto-recorder/backup/ also connects only to the computer viewing it. Allow local network access if requested. The direct localhost page avoids that browser permission. No central remote control, Drive upload or cross-computer settings sync is configured.

## Files and processing

Inside the installation's `Recordings` folder, every capture has its own session folder:

- `original.ts`: original capture, always retained.
- `music-parts/CHANNEL-TIMESTAMP-music-part1ofN.ogg` etc.: instrumental segments of approximately 30 minutes, with a shorter final part.
- `music-parts/manifest.json`: measured lengths, part numbers and total.
- `status.json` and log files: state and errors.
- `audio.wav` and `separated/`: intermediate audio and separated stems.

Demucs uses CPU processing, one job at a time across this installation. First use downloads model weights. Separation can take longer than recording, may leave vocal remnants and may remove instruments. No center-cancellation substitute is used. Interrupted Twitch sessions produce separate folders; they are not merged. Originals may include video when audio-only is unavailable. Parts appear after all have been encoded and checked. Maintain plenty of free disk space; intermediate files can be large.

## Updates and recovery

Stop watching, wait for processing to finish, and close the launcher before updating. Rerun the install command. Only application files are replaced; existing settings, environment and recordings are kept. Older manually extracted installations remain separate: their files are not moved automatically.

For failed processing, use **Retry processing** in the panel. If a watcher was forcibly closed, a channel `.lock` file can remain under Recordings. Only after confirming no backup watcher is running, remove that channel lock and restart. Processing locks are released automatically by the operating system.

## Validation

Automated tests cover exclusive processing across worker processes, watcher stop requests, API authentication, path restrictions and UTF-8 settings. CI runs these on macOS and Windows. Live Twitch capture, third-party installers and actual Demucs separation need testing on each target computer. Never commit recordings, local settings, environments or logs.
