@echo off
REM ============================================================
REM  RAVEN Security Bot - launcher
REM  Starts telegram_bot.py with the project venv when present.
REM  Output is teed to the console and to bot.log.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

title RAVEN Security Bot

REM ---- Resolve an absolute python path -----------------------------
set "PYTHON="
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%CD%\.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    set "PYTHON=%CD%\venv\Scripts\python.exe"
) else (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PYTHON set "PYTHON=%%i"
    )
)

if not defined PYTHON (
    echo.
    echo   [X] Python was not found on PATH, and no .venv is present.
    echo       Install Python 3.10+ or create a venv:
    echo           python -m venv .venv
    echo           .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo   RAVEN Security Bot
echo   ------------------------------------------
echo   Python  : %PYTHON%
echo   Folder  : %CD%
echo   Log     : %CD%\bot.log
echo   ------------------------------------------
echo.

REM ---- Fail fast with a clear message instead of a stack trace ----
"%PYTHON%" -c "import telegram" 2>nul
if errorlevel 1 (
    echo   [X] python-telegram-bot is not installed.
    echo       Run:  "%PYTHON%" -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM ---- Run the bot (logs to console and bot.log) -------------------
"%PYTHON%" telegram_bot.py
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo   [X] RAVEN exited with code %EXITCODE%. Check bot.log for details.
) else (
    echo   [OK] RAVEN stopped cleanly.
)
echo.
REM Only pause for a human who double-clicked this file. When RAVEN_HEADLESS is
REM set (Run_RAVEN_Persistent.bat / Scheduled Task) there is no console to press
REM a key in, and pausing there would hang the service forever.
if not defined RAVEN_HEADLESS pause
endlocal
