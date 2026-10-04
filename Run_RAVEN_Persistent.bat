@echo off
REM ============================================================
REM  RAVEN Security Bot - Persistent Launcher
REM  Restarts the bot after a crash, but a clean exit stops here.
REM ============================================================
setlocal
cd /d "%~dp0"

set "LOG=%CD%\bot.log"
set "MAX_RESTARTS=10"

echo.
echo   RAVEN Security Bot - Persistent Mode
echo   ------------------------------------------
echo   Log:  %LOG%
echo   Press Ctrl+C to stop
echo   ------------------------------------------
echo.

set /a RESTARTS=0

:restart
echo [%date% %time%] Starting RAVEN bot... >> "%LOG%"
call "%~dp0Start_RAVEN_Bot.bat" >> "%LOG%" 2>&1
set "EXITCODE=%ERRORLEVEL%"

REM Exit 0 means the bot shut down on purpose (Ctrl+C, token removed, clean
REM /stop). Restarting there would fight the operator, so only restart on a
REM crash.
if "%EXITCODE%"=="0" goto :done

set /a RESTARTS+=1
if %RESTARTS% gtr %MAX_RESTARTS% goto :exhausted

echo [%date% %time%] RAVEN exited with code %EXITCODE%. Restart %RESTARTS%/%MAX_RESTARTS% in 5s... >> "%LOG%"
timeout /t 5 /nobreak >nul 2>&1
goto :restart

:exhausted
echo.
echo   RAVEN crashed %MAX_RESTARTS% times in a row. Not restarting again.
echo   Last %MAX_RESTARTS% restarts are at the end of %LOG%
echo.
type "%LOG%" 2>nul | more +%RESTARTS%
exit /b 1

:done
echo.
echo   RAVEN stopped cleanly (exit 0). Not restarting.
echo.
endlocal
exit /b 0