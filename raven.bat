@echo off
echo =======================================
echo     RAVEN - Unrestricted Agent
echo =======================================
echo.

REM Check if RAVEN model exists
ollama list | findstr /i "raven" >nul
if %errorlevel% equ 0 (
    echo [+] RAVEN model detected
    
    REM Check if we have a timestamp file for the Modelfile
    if exist .raven_modelfile_timestamp (
        REM Compare Modelfile timestamp with our saved timestamp
        for %%F in (Modelfile) do set "MODFILE_TIME=%%~tF"
        for %%F in (.raven_modelfile_timestamp) do set "SAVED_TIME=%%~tF"
        if "%MODFILE_TIME%" GTR "%SAVED_TIME%" (
            echo [!] Modelfile has been updated - recreating model...
            ollama create raven -f Modelfile
            if not errorlevel 1 (
                echo [+] Model created successfully
                rem Update the timestamp file
                for %%F in (Modelfile) do (
                    echo %%~tF > .raven_modelfile_timestamp
                )
            )
        ) else (
            echo [+] Model is up-to-date with current Modelfile
        )
    ) else (
        echo [!] No timestamp file found - verifying model...
        rem Try to use the model to see if it works
        ollama show raven >nul 2>&1
        if not errorlevel 1 (
            echo [+] Model appears valid
            rem Create timestamp file based on current Modelfile
            if exist Modelfile (
                for %%F in (Modelfile) do (
                    echo %%~tF > .raven_modelfile_timestamp
                )
            )
        ) else (
            echo [!] Model verification failed - recreating...
            ollama create raven -f Modelfile
            if not errorlevel 1 (
                echo [+] Model created successfully
                rem Create timestamp file
                if exist Modelfile (
                    for %%F in (Modelfile) do (
                        echo %%~tF > .raven_modelfile_timestamp
                    )
                )
            )
        )
    )
) else (
    echo [+] Creating RAVEN model (first run - may take a moment)...
    ollama create raven -f Modelfile
    if not errorlevel 1 (
        echo [+] Model created successfully
        rem Create timestamp file
        if exist Modelfile (
            for %%F in (Modelfile) do (
                echo %%~tF > .raven_modelfile_timestamp
            )
        )
    )
)

if errorlevel 1 (
    echo [!] Failed to create or verify model. Is Ollama running?
    echo [!] Try: ollama serve
    pause
    exit /b 1
)

echo [+] Launching RAVEN...
python raven.py %*
echo.
echo =======================================
echo     RAVEN Session Ended
echo =======================================
pause