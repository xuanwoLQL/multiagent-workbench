@echo off
rem ============================================================
rem  Launch the desktop shell directly (no npm wrapper).
rem  Requires `npm install` once so that
rem  node_modules\electron\dist\electron.exe exists.
rem
rem  ASCII only on purpose: cmd.exe parses UTF-8 batch files with
rem  the system code page and mis-splits lines. `chcp 65001` keeps
rem  UTF-8 output from child processes readable in the console.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "ELECTRON=node_modules\electron\dist\electron.exe"
if not exist "%ELECTRON%" (
  echo [multiagent] Electron not found. Run `npm install` in the desktop folder first.
  pause
  exit /b 1
)

echo [multiagent] Starting desktop shell ...
start "" "%ELECTRON%" .
