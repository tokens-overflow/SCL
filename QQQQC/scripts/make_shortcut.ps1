# Q-CC: create a Desktop shortcut that launches the app with the penguin icon.
# Run:  powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1
$ErrorActionPreference = 'Stop'
# this script lives in <root>\scripts\ ; project root is its parent
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$py = (Get-Command python).Source
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'   # no console window
if (-not (Test-Path $pyw)) { $pyw = $py }
$icon = Join-Path $root 'frontend\assets\icon.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Q-CC.lnk'
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $pyw
$s.Arguments = '-m backend.app_native'   # run as module from project root
$s.WorkingDirectory = $root
$s.IconLocation = "$icon,0"
$s.Description = 'Q-CC - Claude Code (QQ2007 skin)'
$s.Save()
Write-Output "created: $lnk"
Write-Output "  target : $pyw -m backend.app_native"
Write-Output "  workdir: $root"
Write-Output "  icon   : $icon"
