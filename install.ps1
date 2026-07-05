Write-Host "=== Installing RAVEN ===" -ForegroundColor Cyan

# Check Ollama
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "[!] Downloading Ollama..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile "$env:TEMP\OllamaSetup.exe"
    Start-Process "$env:TEMP\OllamaSetup.exe" -Wait
    Write-Host "[+] Ollama installed. Restart this script after Ollama runs." -ForegroundColor Green
    exit
}

# Create model
Write-Host "[*] Creating RAVEN model (downloads ~4.7GB first time)..." -ForegroundColor Yellow
ollama create raven -f Modelfile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Failed. Is Ollama running?" -ForegroundColor Red
    exit 1
}

# Install Python dep
Write-Host "[*] Installing Python package..." -ForegroundColor Yellow
pip install ollama

Write-Host "[+] DONE. Run: python raven.py" -ForegroundColor Green
pause
