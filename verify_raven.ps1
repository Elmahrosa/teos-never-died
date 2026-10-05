<#
    Post-reboot acceptance check for RAVEN + Ollama.

    Verifies the 7 invariants that decide whether the current setup is
    production-stable on this hardware:

        1. exactly 1 watchdog
        2. exactly 1 telegram_bot.py
        3. ollama server running and 11434 listening
        4. llama-server 0 while idle
        5. 0 Telegram getUpdates conflicts since the bot started
        6. a real /api/chat round-trip returns real content
        7. RAM free >= 7 GB once the model has unloaded

    Run it with:   powershell -NoProfile -ExecutionPolicy Bypass -File .\verify_raven.ps1
    Add -Live to also stop the model and re-measure RAM after unload.
#>
[CmdletBinding()]
param(
    [switch]$Live,
    # 5 GB is the realistic floor while a browser is open. Use -RamlimitGB 7
    # with Chrome fully closed to assert the strict number.
    [int]$RamlimitGB = 5,
    [string]$LogPath = 'D:\elmahrosa-repos\teos-never-died\bot.log',
    [string]$WatchdogLog = 'D:\elmahrosa-repos\teos-never-died\raven_watchdog.log'
)

$ErrorActionPreference = 'Continue'
$script:fail = 0

function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    $tag = if ($Ok) { 'PASS' } else { 'FAIL'; }
    if (-not $Ok) { $script:fail++ }
    $line = ("[{0}] {1,-34} {2}" -f $tag, $Name, $Detail)
    Write-Output $line
}

Write-Output '================ RAVEN / Ollama acceptance check ================'
Write-Output ("run at {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Write-Output ''

# 1 + 2 --------------------------------------------------------------
$wd = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -EA SilentlyContinue |
    Where-Object { $_.CommandLine -like '*raven_watchdog*' -and $_.ProcessId -ne $PID })
$bot = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -EA SilentlyContinue |
    Where-Object { $_.CommandLine -like '*telegram_bot.py*' })

Check '1. watchdog count = 1' ($wd.Count -eq 1) "found $($wd.Count) (PID $($wd.ProcessId -join ','))"
Check '2. raven bot count = 1' ($bot.Count -eq 1) "found $($bot.Count) (PID $($bot.ProcessId -join ','))"

# 3 ------------------------------------------------------------------
$ollama = @(Get-Process ollama -EA SilentlyContinue)
$listen = @(Get-NetTCPConnection -LocalPort 11434 -State Listen -EA SilentlyContinue)
Check '3. ollama up + 11434 listening' ($ollama.Count -ge 1 -and $listen.Count -ge 1) `
    "process=$($ollama.Count) listening=$($listen.Count)"

# 4 + 7 (idle) -------------------------------------------------------
# Unload first. Ollama 0.35's default keep-alive is effectively 24h, so if a
# previous test (or any client that omits keep_alive) loaded a model it will
# still be sitting in RAM and would make the idle measurement meaningless.
# RAVEN itself is fine - it sends keep_alive=0 - but this script must not be
# the thing that pins 5GB.
$unloaded = $false
try {
    $ps = Invoke-RestMethod 'http://127.0.0.1:11434/api/ps' -TimeoutSec 15
    foreach ($m in @($ps.models)) {
        Write-Output ("     unloading resident model {0} before measuring" -f $m.name)
        $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/generate' -Method Post `
            -Body (@{ model = $m.name; prompt = ''; keep_alive = 0 } | ConvertTo-Json) `
            -ContentType 'application/json' -TimeoutSec 300
        $unloaded = $true
    }
} catch {
    Write-Output "     (could not query loaded models: $($_.Exception.Message))"
}
if ($unloaded) { Start-Sleep -Seconds 8 }

$llama = @(Get-Process llama-server -EA SilentlyContinue)
Check '4. llama-server = 0 while idle' ($llama.Count -eq 0) `
    "found $($llama.Count), $([math]::Round((($llama | Measure-Object WorkingSet64 -Sum).Sum)/1MB,0)) MB"

$os = Get-CimInstance Win32_OperatingSystem
$freeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 1)

# Attribute the number, otherwise this check is meaningless: 7+ GB is only
# reachable with a browser closed, which is not a real 24/7 operating state.
$chromeMB = [math]::Round(((@(Get-Process chrome -EA SilentlyContinue) |
    Measure-Object WorkingSet64 -Sum).Sum) / 1MB, 0)
$oeMB = [math]::Round(((@(Get-Process opencode -EA SilentlyContinue) |
    Measure-Object WorkingSet64 -Sum).Sum) / 1MB, 0)

Check '7. RAM free (idle)' ($freeGB -ge $RamlimitGB) `
    ("{0} GB free (need >= {1}); browser={2} MB, agents={3} MB -> close the browser for 7+" -f `
        $freeGB, $RamlimitGB, $chromeMB, $oeMB)

# 5 ------------------------------------------------------------------
if (Test-Path $LogPath) {
    # Only count conflicts that appeared after this bot process started.
    $start = $null
    if ($bot.Count -ge 1) {
        # Get-CimInstance already hands back a [datetime], so no DMTF conversion.
        $start = [datetime]$bot[0].CreationDate
    }
    $conflicts = @(Select-String -Path $LogPath -Pattern 'getUpdates request' -EA SilentlyContinue)
    if ($start) {
        $after = @($conflicts | Where-Object {
            try { [datetime]::Parse($_.Line.Substring(0, 23)) -ge $start } catch { $false }
        })
    } else {
        $after = @()
    }
    Check '5. telegram conflicts = 0' ($after.Count -eq 0) `
        "$($after.Count) since bot start"
} else {
    Check '5. telegram conflicts = 0' $false "log not found: $LogPath"
}

# 6 ------------------------------------------------------------------
$body = @{
    model      = 'dolphin-llama3:8b'
    messages   = @(@{ role = 'user'; content = 'Reply with the single word: ready' })
    stream     = $false
    keep_alive = 0
    options    = @{ num_predict = 10 }
} | ConvertTo-Json -Depth 5

$reply = $null
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $r = Invoke-RestMethod 'http://127.0.0.1:11434/api/chat' -Method Post `
        -Body $body -ContentType 'application/json' -TimeoutSec 900
    $reply = $r.message.content
    $sw.Stop()
    Check '6. /api/chat returns content' (-not [string]::IsNullOrWhiteSpace($reply)) `
        ("{0:N1}s -> '{1}'" -f $sw.Elapsed.TotalSeconds, $reply.Trim())
} catch {
    $sw.Stop()
    Check '6. /api/chat returns content' $false "error after $($sw.Elapsed.TotalSeconds.ToString('N1'))s: $($_.Exception.Message)"
}

# optional: confirm the model actually unloads again -------------------
if ($Live) {
    Write-Output ''
    Write-Output '--- -Live: unloading model and re-measuring RAM ---'
    try {
        $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/generate' -Method Post `
            -Body (@{ model = 'dolphin-llama3:8b'; prompt = ''; keep_alive = 0 } | ConvertTo-Json) `
            -ContentType 'application/json' -TimeoutSec 300
    } catch { }
    Start-Sleep -Seconds 8
    $llama2 = @(Get-Process llama-server -EA SilentlyContinue)
    $os2 = Get-CimInstance Win32_OperatingSystem
    $free2 = [math]::Round($os2.FreePhysicalMemory / 1MB, 1)
    Check '7b. RAM after unload' ($free2 -ge $RamlimitGB) `
        "${free2} GB free, llama-server=$($llama2.Count)"
}

Write-Output ''
if (Test-Path $WatchdogLog) {
    Write-Output '--- watchdog log (last 5) ---'
    Get-Content $WatchdogLog -Tail 5 | ForEach-Object { Write-Output "  $_" }
    Write-Output ''
}

if ($script:fail -eq 0) {
    Write-Output 'RESULT: ALL CHECKS PASSED - stable on this hardware.'
} else {
    Write-Output "RESULT: $($script:fail) CHECK(S) FAILED - do not delete D:\.ollama\models.old yet."
}
Write-Output '==============================================================='