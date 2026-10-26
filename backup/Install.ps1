$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$InstallDir = Join-Path $env:LOCALAPPDATA 'TwitchBackupRecorder'
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$Source = 'https://raw.githubusercontent.com/ewanders1-web/twitch-auto-recorder/main/backup'
foreach ($File in @('recorder.py', 'interface.py', 'index.html', 'requirements.txt', 'README.md', 'Start Backup.cmd')) {
    $Target = Join-Path $InstallDir $File
    Invoke-WebRequest -UseBasicParsing -Uri "$Source/$([uri]::EscapeDataString($File))" -OutFile "$Target.download"
    Move-Item -Force -LiteralPath "$Target.download" -Destination $Target
}
# Locate Python 3.11, including before an installer has refreshed this terminal's PATH.
function Find-Python {
    $Candidates = @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'), (Join-Path $env:ProgramFiles 'Python311\python.exe'))
    $Command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($Command -and $Command.Source -notlike '*\WindowsApps\*') { $Candidates += $Command.Source }
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            $Version = & $Candidate -c 'import sys; print("%s.%s" % sys.version_info[:2])' 2>$null
            if ($LASTEXITCODE -eq 0 -and $Version -eq '3.11') { return $Candidate }
        }
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw 'Install Python 3.11 from python.org (enable the Python launcher), then run this installer again. Or install App Installer from the Microsoft Store to enable winget.'
    }
    & winget.exe install --id Python.Python.3.11 --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Python installation failed. Install Python 3.11 and retry.' }
    $Python = Find-Python
    if (-not $Python) { throw 'Python 3.11 was installed. Close this window and run the installer again.' }
}
function Find-FFmpeg {
    $Command = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($Command -and (Test-Path (Join-Path (Split-Path $Command.Source) 'ffprobe.exe'))) { return (Split-Path $Command.Source) }
    $Packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (Test-Path $Packages) {
        $Found = Get-ChildItem -Path "$Packages\Gyan.FFmpeg*" -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($Found -and (Test-Path (Join-Path $Found.DirectoryName 'ffprobe.exe'))) { return $Found.DirectoryName }
    }
    return $null
}
$FFmpeg = Find-FFmpeg
if (-not $FFmpeg) {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw 'Install FFmpeg and ffprobe, add their bin folder to PATH, then run this installer again.' }
    & winget.exe install --id Gyan.FFmpeg --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'FFmpeg installation failed. Install FFmpeg and retry.' }
    $FFmpeg = Find-FFmpeg
    if (-not $FFmpeg) { throw 'Close this window and rerun the installer so FFmpeg can be found.' }
}
Set-Content -LiteralPath (Join-Path $InstallDir 'ffmpeg-path.txt') -Value $FFmpeg -Encoding Unicode
Set-Location -LiteralPath $InstallDir
& $Python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the private Python environment.' }
$VenvPython = Join-Path $InstallDir '.venv\Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not update pip.' }
& $VenvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your internet connection and rerun this installer.' }
$env:PATH = "$InstallDir\.venv\Scripts;$FFmpeg;$env:PATH"
& $VenvPython recorder.py check
if ($LASTEXITCODE -ne 0) { throw 'Recorder checks failed. Review the messages above.' }
Write-Host "Ready. Recordings will be saved in $InstallDir\Recordings."
& (Join-Path $InstallDir 'Start Backup.cmd')
