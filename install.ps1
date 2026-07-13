Write-Host "=== Installing RAVEN ===" -ForegroundColor Cyan

$OLLAMA_URL = "https://ollama.com/download/OllamaSetup.exe"
$INSTALLER = "$env:TEMP\OllamaSetup.exe"

# --- Install Ollama ---
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {

    # Try winget first (official, verified)
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "[*] Installing Ollama via winget..." -ForegroundColor Yellow
        winget install Ollama.Ollama --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -eq 0) {
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
            Write-Host "[+] Ollama installed via winget." -ForegroundColor Green
        } else {
            Write-Host "[!] winget failed, falling back to verified download..." -ForegroundColor Yellow
        }
    }

    # Fallback: download with hash verification
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Write-Host "[*] Downloading Ollama installer..." -ForegroundColor Yellow
        try {
            Invoke-WebRequest -Uri $OLLAMA_URL -OutFile $INSTALLER -UseBasicParsing
        } catch {
            Write-Host "[!] Download failed: $_" -ForegroundColor Red
            exit 1
        }

        # Fetch expected SHA256 from Ollama GitHub releases
        Write-Host "[*] Verifying installer hash..." -ForegroundColor Yellow
        try {
            $release = Invoke-RestMethod -Uri "https://api.github.com/repos/ollama/ollama/releases/latest" -UseBasicParsing
            $asset = $release.assets | Where-Object { $_.name -eq "OllamaSetup.exe" } | Select-Object -First 1
            $expectedHash = ($release.body -split "`n") |
                Where-Object { $_ -match "OllamaSetup\.exe.*?([a-fA-F0-9]{64})" } |
                ForEach-Object { if ($matches[1]) { $matches[1] } }

            if (-not $expectedHash -and $asset) {
                $expectedHash = $asset.browser_download_url
                # Some releases put hash in the body near the filename
                $line = ($release.body -split "`n") | Where-Object { $_ -match "OllamaSetup" }
                if ($line -match "([a-fA-F0-9]{64})") { $expectedHash = $matches[1] }
            }
        } catch {
            Write-Host "[!] Could not fetch release info: $_" -ForegroundColor Yellow
            $expectedHash = $null
        }

        $actualHash = (Get-FileHash $INSTALLER -Algorithm SHA256).Hash

        if ($expectedHash) {
            if ($actualHash -ne $expectedHash.ToUpper()) {
                Write-Host "[!] HASH MISMATCH!" -ForegroundColor Red
                Write-Host "    Expected: $expectedHash" -ForegroundColor Red
                Write-Host "    Got:      $actualHash" -ForegroundColor Red
                Remove-Item $INSTALLER -Force
                exit 1
            }
            Write-Host "[+] Hash verified: $actualHash" -ForegroundColor Green
        } else {
            Write-Host "[~] Could not verify hash (no release data). Proceeding..." -ForegroundColor Yellow
            Write-Host "    SHA256: $actualHash" -ForegroundColor Gray
        }

        Write-Host "[*] Running installer..." -ForegroundColor Yellow
        Start-Process $INSTALLER -Wait
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        Write-Host "[+] Ollama installed." -ForegroundColor Green
    }
}

# --- Create model ---
Write-Host "[*] Creating RAVEN model (downloads ~4.7GB first time)..." -ForegroundColor Yellow
ollama create raven -f Modelfile
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Failed. Is Ollama running?" -ForegroundColor Red
    exit 1
}

# --- Install Python dep ---
Write-Host "[*] Installing Python package..." -ForegroundColor Yellow
pip install ollama

Write-Host "[+] DONE. Run: python raven.py" -ForegroundColor Green
pause
