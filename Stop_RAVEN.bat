@echo off
REM RAVEN Security Bot - targeted stop (only telegram_bot.py)
setlocal
cd /d "%~dp0"
echo.
echo   Stopping RAVEN...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | Where-Object { $_.CommandLine -match 'telegram_bot\.py' }; if (-not $procs) { Write-Output 'NO_PROCESS'; exit 0 }; foreach ($p in $procs) { Write-Output ('Killing PID ' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }"
if errorlevel 1 (
    echo.
    echo   [X] Stop failed. Try running this file as Administrator.
) else (
    echo.
    echo   [OK] RAVEN stopped.
)
echo.
ping -n 4 127.0.0.1 >nul
endlocal
