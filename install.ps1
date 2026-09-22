# Twitch Auto-Recorder — portable install (Windows)
# Usage (PowerShell):
#   irm https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/install.ps1 | iex
#   .\install.ps1 -Start
param(
  [switch]$Start
)

$ErrorActionPreference = "Stop"
$Repo = "ewanders1-web/twitch-auto-recorder"
$Branch = "main"
$Raw = "https://raw.githubusercontent.com/$Repo/$Branch"
$Files = @(
  "twitch-auto-recorder.html",
  "twitch-recorder-server.py",
  "README-recorder.txt",
  "VERSION"
)

$InstallDir = Join-Path $env:LOCALAPPDATA "TwitchRecorder"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Write-Host "Installing Twitch Auto-Recorder into:"
Write-Host "  $InstallDir"
Write-Host ""

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("twitch-recorder-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  foreach ($f in $Files) {
    Write-Host "  downloading $f …"
    $dest = Join-Path $tmp $f
    Invoke-WebRequest -Uri "$Raw/$f" -OutFile $dest -UseBasicParsing
  }
  foreach ($f in $Files) {
    Copy-Item -Force (Join-Path $tmp $f) (Join-Path $InstallDir $f)
  }
  Copy-Item -Force (Join-Path $InstallDir "twitch-auto-recorder.html") (Join-Path $InstallDir "index.html")
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

$ver = "unknown"
$verPath = Join-Path $InstallDir "VERSION"
if (Test-Path $verPath) { $ver = (Get-Content $verPath -Raw).Trim() }

Write-Host ""
Write-Host "Installed version: $ver"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) pip install streamlink"
Write-Host "     (optional: pip install demucs  +  ffmpeg on PATH)"
Write-Host "  2) Start the helper:"
Write-Host "       cd `"$InstallDir`""
Write-Host "       python twitch-recorder-server.py"
Write-Host "  3) Open the UI: https://ewanders1-web.github.io/twitch-auto-recorder/"
Write-Host "     Or locally: http://127.0.0.1:8765/"
Write-Host ""
Write-Host "Recordings: %USERPROFILE%\TwitchRecordings  |  Helper: 127.0.0.1:8765 only"
Write-Host ""

if ($Start) {
  $py = Get-Command python -ErrorAction SilentlyContinue
  if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
  if (-not $py) {
    Write-Error "Python not found — install Python 3, then re-run with -Start."
  }
  $log = Join-Path $InstallDir "helper.log"
  $script = Join-Path $InstallDir "twitch-recorder-server.py"
  Start-Process -FilePath $py.Source -ArgumentList $script -WorkingDirectory $InstallDir -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $log
  Write-Host "Helper started in background. Log: $log"
  Write-Host "Health: http://127.0.0.1:8765/api/health"
}
