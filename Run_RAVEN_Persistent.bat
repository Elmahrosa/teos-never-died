@echo off
REM ============================================================
REM  RAVEN Security Bot - Headless Launcher
REM
REM  Runs the bot once and exits with the bot's own exit code.
REM  This script deliberately does NOT contain a restart loop:
REM  auto-restart and start-at-boot are provided by the
REM  "RAVEN Bot" Scheduled Task. Two restart mechanisms would
REM  fight each other and hide real crash reasons.
REM
REM  Safe to run with no console and no user interaction:
REM  * RAVEN_HEADLESS suppresses Start_RAVEN_Bot.bat's "press any key"
REM  * rAVEN_launcher.log is written here; bot.log belongs to
REM    telegram_bot.py's own logging.FileHandler and is NOT
REM    redirected into, because two writers on one file made the
REM    Python handler fail with "Permission denied" at startup.
REM ============================================================
setlocal
cd /d "%~dp0"

set "RAVEN_HEADLESS=1"
set "LAUNCH_LOG=%CD%\raven_launcher.log"

REM ---- Resolve an absolute python path -----------------------------
set "PYTHON="
if exist "%CD%\.venv\Scripts\python.exe" set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not defined PYTHON if exist "%CD%\venv\Scripts\python.exe" set "PYTHON=%CD%\venv\Scripts\python.exe"
if not defined PYTHON (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYTHON set "PYTHON=%%i"
    )
)

if not defined PYTHON (
    echo [%date% %time%] FATAL: no python interpreter found >> "%LAUNCH_LOG%"
    exit /b 1
)

echo [%date% %time%] starting RAVEN with %PYTHON% >> "%LAUNCH_LOG%"

REM ---- Run the bot. Its own logging.FileHandler owns bot.log. -------
"%PYTHON%" telegram_bot.py
set "EXITCODE=%ERRORLEVEL%"

echo [%date% %time%] RAVEN exited with code %EXITCODE% >> "%LAUNCH_LOG%"
endlocal & exit /b %EXITCODE%