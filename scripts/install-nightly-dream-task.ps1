[CmdletBinding(SupportsShouldProcess)]
param(
  [Parameter(Mandatory)][string[]]$Owner,
  [string]$TaskName = 'Common AI Memory - Nightly Dream Prepare',
  [string]$LocalTime = '02:30',
  [string]$Timezone = 'Asia/Shanghai',
  [switch]$WakeToRun
)
$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run-nightly-dreams.ps1'
$ownerList = ($Owner | ForEach-Object { $_ -replace '[^A-Za-z0-9_-]','' }) -join ','
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -Owners `"$ownerList`" -Timezone `"$Timezone`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Daily -At $LocalTime
$settingsArgs = @{StartWhenAvailable=$true; MultipleInstances='IgnoreNew'; ExecutionTimeLimit=(New-TimeSpan -Minutes 45)}
if ($WakeToRun) { $settingsArgs.WakeToRun = $true }
$settings = New-ScheduledTaskSettingsSet @settingsArgs
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
if ($PSCmdlet.ShouldProcess($TaskName, 'Register optional nightly Dream task')) {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Prepare derived Dream materials or run explicitly configured per-owner runners.' -Force | Out-Null
}
