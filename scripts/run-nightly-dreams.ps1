[CmdletBinding()]
param(
  [Parameter(Mandatory)][string]$Owners,
  [string]$Timezone = 'Asia/Shanghai',
  # Defaults to DATA_DIR from .env (./runtime), the same data directory as the MCP server.
  [string]$ProjectRoot = '',
  # Defaults to this project's .venv interpreter.
  [string]$Python = '',
  [switch]$DryRun
)
# Exit codes (from dream_nightly.py): 0 all done, 4 some owners fell back to on_wake,
# 5 every CLI owner fell back, 2 infrastructure failure, 3 another run holds the lock.
$ErrorActionPreference = 'Stop'
$codeRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $codeRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw 'Project virtual environment is missing.' }
$entry = Join-Path $codeRoot 'dream_nightly.py'
# A source checkout runs the file; an installed package runs the module.
$arguments = @()
if (Test-Path -LiteralPath $entry) { $arguments += $entry } else { $arguments += @('-m', 'dream_nightly') }
if ($ProjectRoot) { $arguments += @('--project-root', $ProjectRoot) }
$arguments += @('--timezone', $Timezone)
foreach ($name in ($Owners -split ',')) {
  $name = $name.Trim()
  if ($name) { $arguments += @('--owner', $name) }
}
if ($DryRun) { $arguments += '--dry-run' }
Push-Location $codeRoot
try { & $Python @arguments; $code = $LASTEXITCODE } finally { Pop-Location }
exit $code
