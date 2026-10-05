@echo off
setlocal EnableExtensions EnableDelayedExpansion
title RAVEN - Unrestricted Agent
cd /d "%~dp0"

REM Resolve both tools from PATH. These used to be hardcoded to a different
REM Windows account (C:\Users\Elmahrosa-Teos\...) and a Python version that
REM is not installed here, so this launcher could never start on this machine.
set "PYEXE="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%i"

set "OLLAMA_EXE="
for /f "delims=" %%i in ('where ollama 2^>nul') do if not defined OLLAMA_EXE set "OLLAMA_EXE=%%i"

echo =======================================
echo     RAVEN - Unrestricted Agent
echo =======================================
echo.

REM --- Sanity checks ---
where ollama >nul 2>nul
if errorlevel 1 (
  echo [!] Ollama not found on PATH.
  echo     Install from https://ollama.com and re-run.
  goto :end
)

if not exist "%PYEXE%" (
  echo [!] Python not found at: %PYEXE%
  goto :end
)

REM --- Ensure ollama daemon is reachable ---
"%OLLAMA_EXE%" list >nul 2>nul
if errorlevel 1 (
  echo [*] Ollama daemon not responding. Starting it...
  start "" "%OLLAMA_EXE%" serve
  timeout /t 4 >nul
)

REM --- Health check: model present AND loadable ---
REM `ollama show` succeeds only if the manifest exists AND its blobs are on disk.
"%OLLAMA_EXE%" show raven >nul 2>nul
if errorlevel 1 (
  echo [*] Model 'raven' missing or broken. Building from Modelfile...
  "%OLLAMA_EXE%" create raven -f Modelfile
  if errorlevel 1 (
    echo [!] Failed to build 'raven' model.
    echo     Run manually: ollama create raven -f Modelfile
    goto :end
  )
) else (
  echo [*] Model 'raven' is healthy - skipping rebuild.
)
echo.

REM --- Launch agent ---
echo [*] Starting RAVEN ...
"%PYEXE%" raven.py
set RC=%ERRORLEVEL%

:end
echo.
if not "%RC%"=="0" echo [!] RAVEN exited with code %RC%.
pause
endlocal
