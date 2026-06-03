@echo off
echo =======================================
echo     RAVEN - Unrestricted Agent
echo =======================================
echo.

ollama create raven -f Modelfile

echo Model created successfully!
echo.
echo Starting RAVEN...
echo.

python raven.py

pause
