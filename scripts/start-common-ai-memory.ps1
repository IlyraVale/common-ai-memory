[CmdletBinding()]
param(
  [int]$Port = 8877,
  [string]$Python = '',
  [switch]$Open,
  [switch]$NoTask
)
# Optional Windows helper. Brings up the unified Memory UI (game hall and lounge
# included) unless it is already healthy, then prints every service's state.
# The data directory comes from DATA_DIR in .env (default ./runtime).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
  $venv = Join-Path $root '.venv\Scripts\python.exe'
  $Python = if (Test-Path -LiteralPath $venv) { $venv } else { 'python' }
}
$arguments = @((Join-Path $root 'memory_services.py'), 'start', '--port', $Port)
if ($Open) { $arguments += '--open' }
if ($NoTask) { $arguments += '--no-task' }
Push-Location $root
try { & $Python @arguments; exit $LASTEXITCODE } finally { Pop-Location }
