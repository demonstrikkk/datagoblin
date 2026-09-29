<#
.SYNOPSIS
  Starts ONLY the local LLM server, with its password read from .env.

.DESCRIPTION
  Exists so the password is never typed, and never pasted from a message.

  A run failed end to end because the server had been started with the literal
  text `<copy OPENCODE_PASSWORD from .env>` — a placeholder that had been copied
  from a comment instead of replaced with the real value. The server was
  perfectly happy with that password, so it started, printed "listening", and
  answered every request with 401 once the API sent the real one. Three minutes
  and twelve successful fetches later the run returned 0 records.

  The whole failure was one manual copy step. This script removes it: the
  password is read from the same .env the API reads, so the two cannot disagree
  about what the password is.

.EXAMPLE
  powershell -File scripts\llm.ps1
  powershell -File scripts\llm.ps1 -Port 4096
#>
[CmdletBinding()]
param(
  [int]$Port = 4096,
  [string]$Host_ = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $Root ".env"
$LogDir = Join-Path $Root "outputs\logs"

if (-not (Test-Path $envFile)) {
  Write-Host "  no .env at $envFile - nothing to read a password from" -ForegroundColor Red
  exit 1
}
$line = Select-String -Path $envFile -Pattern "^OPENCODE_PASSWORD=" -ErrorAction SilentlyContinue |
        Select-Object -First 1
if (-not $line) {
  Write-Host "  .env has no OPENCODE_PASSWORD line." -ForegroundColor Yellow
  Write-Host "  Starting without a password; the API will send no auth header." -ForegroundColor Yellow
} else {
  # Read the same way the API's own dotenv loader will, so the value the server
  # gets and the value the API sends are byte-identical by construction.
  $env:OPENCODE_SERVER_PASSWORD = ($line.Line -split "=", 2)[1].Trim()
  Write-Host "  password loaded from .env (OPENCODE_PASSWORD, $((($env:OPENCODE_SERVER_PASSWORD).Length)) chars)"
}

$oc = (Get-Command opencode -ErrorAction SilentlyContinue)
if (-not $oc) {
  Write-Host "  `opencode` is not on PATH. Install it, then re-run." -ForegroundColor Red
  exit 1
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Write-Host "  starting opencode serve on $Host_`:$Port ..."
& opencode serve --port $Port --hostname $Host_
