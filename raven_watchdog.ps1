<#
    RAVEN watchdog - unattended supervisor.

    Why this exists: registering a Windows Scheduled Task with
    "restart on failure" + "run whether user is logged on or not" needs
    elevation, which is not always available. This watchdog provides the
    same guarantees using only user-level privileges:

      * keeps the bot running if it crashes
      * starts at user logon (registered in the HKCU Run key)
      * distinguishes a crash from a deliberate clean exit
      * guards against a crash loop burning CPU forever
      * never prompts; safe with no console and no keyboard

    Start it with no visible window:
      powershell -WindowStyle Hidden -File .\raven_watchdog.ps1
#>

[CmdletBinding()]
param(
    # NOTE: every parameter below carries a trailing comma. Windows PowerShell
    # 5.1 mis-parses a comment line that follows a parameter without one
    # ("Missing ')' in function parameter list"), so the commas are required,
    # not stylistic.

    # Short delay after a crash.
    [int]$CrashDelaySec = 15,
    # Longer delay after a clean exit, so a deliberate stop is respected but
    # still recovers on its own.
    [int]$CleanDelaySec = 60,
    # Crash-loop guard: if this many restarts happen inside the window, back off.
    [int]$LoopThreshold = 20,
    [int]$LoopWindowSec = 300,
    [int]$LoopBackoffSec = 300
)

$ErrorActionPreference = 'Continue'
$root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher   = Join-Path $root 'Run_RAVEN_Persistent.bat'
$logPath    = Join-Path $root 'raven_watchdog.log'
$lockPath   = Join-Path $root 'raven_watchdog.lock'

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    # Append with retry: the launcher may briefly hold the file.
    for ($i = 0; $i -lt 5; $i++) {
        try {
            Add-Content -LiteralPath $logPath -Value "[$ts] [$Level] $Message" -Encoding UTF8 -ErrorAction Stop
            return
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
}

# ---- single-instance guard -------------------------------------------------
# Without this, the Run key at logon plus a manual launch would produce two
# watchdogs, each restarting the bot, and they would fight over the port/token.
$lockFd = $null
try {
    $lockFd = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
} catch {
    Write-Log 'another watchdog already holds the lock; exiting' 'WARN'
    exit 0
}

try {
    Write-Log "watchdog starting (pid $PID); launcher: $launcher"

    $restarts = New-Object System.Collections.Queue

    while ($true) {
        # Never stack instances: if a bot is already alive, just wait and re-check.
        # Get-Process has no CommandLine property in Windows PowerShell 5.1, so
        # use CIM to match on telegram_bot.py specifically and avoid matching
        # unrelated python processes.
        $running = $false
        try {
            $running = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" `
                -ErrorAction Stop |
                Where-Object { $_.CommandLine -like '*telegram_bot.py*' }).Count -gt 0
        } catch {
            # If we cannot tell, assume it is running rather than risk a
            # second instance fighting over the Telegram token.
            $running = @(Get-Process python -ErrorAction SilentlyContinue).Count -gt 0
            if (-not $running) {
                Write-Log "could not inspect processes ($($_.Exception.Message)); assuming none running" 'WARN'
            }
        }

        if ($running) {
            Start-Sleep -Seconds 30
            continue
        }

        Write-Log 'launching RAVEN'
        $proc = Start-Process -FilePath 'cmd.exe' `
            -ArgumentList '/c', "`"$launcher`"" `
            -WorkingDirectory $root `
            -WindowStyle Hidden `
            -PassThru `
            -Wait

        $code = $proc.ExitCode
        if ($code -eq 0) {
            Write-Log 'RAVEN exited cleanly (0); will restart after a delay' 'INFO'
            Start-Sleep -Seconds $CleanDelaySec
        } else {
            Write-Log "RAVEN crashed with exit code $code; restarting in ${CrashDelaySec}s" 'ERROR'
            Start-Sleep -Seconds $CrashDelaySec
        }

        # ---- crash-loop guard ------------------------------------------------
        $now = Get-Date
        while ($restarts.Count -gt 0 -and
               ($now - $restarts.Peek()).TotalSeconds -gt $LoopWindowSec) {
            $null = $restarts.Dequeue()
        }
        $restarts.Enqueue($now)

        if ($restarts.Count -ge $LoopThreshold) {
            Write-Log "crash loop: $LoopThreshold restarts within ${LoopWindowSec}s; backing off ${LoopBackoffSec}s" 'ERROR'
            Start-Sleep -Seconds $LoopBackoffSec
            $restarts.Clear()
        }
    }
}
catch {
    Write-Log "watchdog crashed: $($_.Exception.Message)" 'ERROR'
    exit 1
}
finally {
    if ($lockFd) { $lockFd.Close() }
    Write-Log 'watchdog exiting'
}