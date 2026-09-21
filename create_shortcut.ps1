$ErrorActionPreference = 'Stop'
$Desktop = [Environment]::GetFolderPath('Desktop')
$ShortcutPath = Join-Path $Desktop 'RAVEN.lnk'
$Target = 'C:\Users\Elmahrosa-Teos\Desktop\teos-never-died\launch_raven.bat'
$WorkDir = 'C:\Users\Elmahrosa-Teos\Desktop\teos-never-died'
$Icon = 'C:\Users\Elmahrosa-Teos\Desktop\teos-never-died\raven.bat'  # fallback; replaced below if ico exists
# Prefer a custom .ico if the user ever drops one in
$customIco = Join-Path $WorkDir 'raven.ico'
if (Test-Path $customIco) { $Icon = $customIco } else { $Icon = 'C:\Windows\System32\shell32.dll,12' }

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $WorkDir
$Shortcut.WindowStyle = 1
$Shortcut.IconLocation = $Icon
$Shortcut.Description = 'RAVEN - Unrestricted Agent (TEOS)'
$Shortcut.Save()

Write-Output "Created: $ShortcutPath"
