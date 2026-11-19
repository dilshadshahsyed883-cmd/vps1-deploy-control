@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-MyRemote.ps1"
set "result=%ERRORLEVEL%"
if not "%result%"=="0" (
  echo.
  echo My Remote could not start. Review the error above.
  pause
)
exit /b %result%
