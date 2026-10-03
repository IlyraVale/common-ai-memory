[CmdletBinding(SupportsShouldProcess)]
param(
  [int]$Port = 8877,
  [string]$TaskName = 'Common AI Memory - UI',
  [string]$Pythonw = ''
)
# Optional Windows helper: start the unified Memory UI at logon, hidden, for the
# current user. Runs pythonw (no console window); the UI refuses to start a second
# copy (it checks /health) and keeps the lounge bridge worker running.
# No administrator rights needed. Remove with: Unregister-ScheduledTask -TaskName $TaskName
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Pythonw) { $Pythonw = Join-Path $root '.venv\Scripts\pythonw.exe' }
if (-not (Test-Path -LiteralPath $Pythonw)) { throw 'pythonw.exe not found; pass -Pythonw <path>.' }
$entry = Join-Path $root 'memory_ui.py'
$arguments = "`"$entry`" --port $Port --supervise-bridge"
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
if ($PSCmdlet.ShouldProcess($TaskName, 'Register Memory UI logon task')) {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Unified Common AI Memory UI (localhost only).' -Force | Out-Null
  Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
}
