@echo off
setlocal
cd /d "%LOCALAPPDATA%\TwitchBackupRecorder"
if not exist ".venv\Scripts\python.exe" (
  echo Run Install.cmd first.
  pause
  exit /b 1
)
set "PATH=%CD%\.venv\Scripts;%PATH%"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" interface.py
if errorlevel 1 pause
