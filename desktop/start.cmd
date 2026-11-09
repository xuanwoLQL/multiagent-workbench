@echo off
rem ============================================================
rem  Desktop dev launcher (ASCII only on purpose).
rem
rem  cmd.exe parses .cmd files byte-by-byte with the console code
rem  page (936/GBK on zh-CN). A UTF-8 batch file with Chinese text
rem  makes cmd.exe drift and split lines (e.g. "if errorlevel" is
rem  read as "orlevel"), even after `chcp 65001`. Verified with
rem  cmd /c on Windows 11 zh-CN. Keep this file pure ASCII.
rem
rem  `chcp 65001` is still needed: child processes (npm / node /
rem  electron) emit UTF-8, and the console must decode it as UTF-8
rem  so any Chinese output from them is not mojibake.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist node_modules (
  echo [multiagent] First run: installing Electron dependencies, this may take a few minutes ...
  call npm install
  if errorlevel 1 (
    echo [multiagent] npm install failed. Check network / npm registry, then retry.
    pause
    exit /b 1
  )
)

echo [multiagent] Starting desktop shell in dev mode ...
call npm start
if errorlevel 1 (
  echo [multiagent] Desktop shell failed to start.
  echo [multiagent] Backend log: .multiagent\logs\desktop-backend.log
  pause
)
