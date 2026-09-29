<#
.SYNOPSIS
  Starts (or stops) the three processes this app needs: the local LLM server,
  the API, and the frontend.

.DESCRIPTION
  Each of the three has to be running for anything to work, and until now
  nothing said so:

  * The LLM server is required for extraction. When it is down, runs and
    backfills fail closed with a provider error — honest, but it looks like the
    app is broken rather than like a missing dependency.
  * The LLM server reports a port clash as a bare `Error: Unexpected error /
    ServeError`, with no mention of the port. Starting a second one by hand
    while the first is up produces exactly that, and it reads as a broken
    install.
  * Nothing survives the shell that launched it. Starting the API in one
    terminal and the frontend in another is the only reliable way to keep both
    up, which is why "the server died" kept happening.

  This script owns all three, reports exactly what it did, and tells you where
  the logs are.

.PARAMETER Stop
  Stop everything this script started, plus any process holding the ports.

.PARAMETER Port
  API port. Default 8000.

.EXAMPLE
  powershell -File scripts\dev.ps1
  powershell -File scripts\dev.ps1 -Stop
#>
[CmdletBinding()]
param(
  [switch]$Stop,
  [int]$ApiPort = 8000,
  [int]$WebPort = 4173,
  [string]$LlmPort = "4096"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "outputs\logs"
$envFile = Join-Path $Root ".env"

function Get-Listener([int]$Port) {
  $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($c) { return $c[0].OwningProcess } else { return $null }
}

function Get-Name([int]$Pid_) {
  try { return (Get-Process -Id $Pid_ -ErrorAction Stop).ProcessName } catch { return "?" }
}

function Read-EnvValue([string]$Key) {
  if (-not (Test-Path $envFile)) { return $null }
  $line = Select-String -Path $envFile -Pattern "^$Key=" -ErrorAction SilentlyContinue |
          Select-Object -First 1
  if (-not $line) { return $null }
  return ($line.Line -split "=", 2)[1].Trim()
}

function Wait-Http([string]$Url, [int]$Seconds = 45) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest $Url -TimeoutSec 5 -UseBasicParsing
      if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { return $true }
    } catch {
      $code = $null
      try { $code = $_.Exception.Response.StatusCode.value__ } catch { }
      # 401/403 still means something is listening and speaking.
      if ($code -ge 200 -and $code -lt 500) { return $true }
    }
    Start-Sleep -Milliseconds 700
  }
  return $false
}

function Test-LlmAuth([string]$Password, [int]$Port) {
  <#
    Can the API actually authenticate to the LLM server?

    Wait-Http only proves something is listening, and opencode answers 401 to
    everything when its password does not match the client's. So "the server is
    up" was never the same claim as "extraction will work", and a run that
    believed the first one spent three minutes and twelve successful fetches
    before discovering it was false — then returned 0 records with the real
    reason buried in a log file.

    This asks the question the run will actually ask.
  #>
  if (-not $Password) { return $true }   # no password configured, none required
  $pair = "opencode:$Password"
  $bytes = [System.Text.Encoding]::ASCII.GetBytes($pair)
  $token = [System.Convert]::ToBase64String($bytes)
  try {
    $r = Invoke-WebRequest "http://127.0.0.1:$Port/session" -TimeoutSec 10 `
         -UseBasicParsing -Headers @{ Authorization = "Basic $token" }
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
  } catch {
    return $false
  }
}

if ($Stop) {
  foreach ($spec in @(@($ApiPort, "api"), @($WebPort, "frontend"), @([int]$LlmPort, "llm"))) {
    $pid_ = Get-Listener $spec[0]
    if ($pid_) {
      Write-Host "  stopping $($spec[1]) on port $($spec[0]) (pid $pid_)"
      Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
    } else {
      Write-Host "  $($spec[1]) is not listening on $($spec[0])"
    }
  }
  exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Write-Host "datagoblin dev services" -ForegroundColor Cyan
Write-Host "  logs: $LogDir"

# --- 1. the LLM server -------------------------------------------------------
$llm = Get-Listener ([int]$LlmPort)
if ($llm) {
  # The single most confusing failure in this setup. Say who is holding it
  # rather than letting `opencode serve` print `ServeError` and exit.
  $pw = Read-EnvValue "OPENCODE_PASSWORD"
  if (-not (Test-LlmAuth $pw ([int]$LlmPort))) {
    # Reusing a server that is up but unreachable-by-auth is how the last
    # failure happened: dev.ps1 reported "already listening - reusing it", every
    # extraction 401'd, and nothing in the output said the two passwords differed.
    Write-Host "  llm      : FAILED AUTH - something is already on $LlmPort but it" -ForegroundColor Red
    Write-Host "             rejects the OPENCODE_PASSWORD in .env. That is a" -ForegroundColor Red
    Write-Host "             different server, or one started with a different" -ForegroundColor Red
    Write-Host "             password. Take it over first:" -ForegroundColor Red
    Write-Host "               powershell -File scripts\dev.ps1 -Stop" -ForegroundColor Red
    exit 1
  }
  Write-Host "  llm      : already listening on $LlmPort (pid $llm, $(Get-Name $llm)) - reusing it, auth OK"
} else {
  $oc = (Get-Command opencode -ErrorAction SilentlyContinue)
  if (-not $oc) {
    Write-Host "  llm      : FAILED - `opencode` is not on PATH. Install it, then re-run." -ForegroundColor Red
    exit 1
  }
  $pw = Read-EnvValue "OPENCODE_PASSWORD"
  if ($pw) { $env:OPENCODE_SERVER_PASSWORD = $pw }
  Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/c", "opencode serve --port $LlmPort --hostname 127.0.0.1 > `"$LogDir\llm.log`" 2>&1" `
    -WindowStyle Hidden
  if (Wait-Http "http://127.0.0.1:$LlmPort" 30) {
    if (-not (Test-LlmAuth $pw ([int]$LlmPort))) {
      # Refuse to go further. Starting the API and the frontend on top of an
      # LLM server we cannot authenticate against produces a working-looking
      # app whose every extraction fails.
      Write-Host "  llm      : FAILED AUTH - the server is up but rejects the" -ForegroundColor Red
      Write-Host "             password in .env (OPENCODE_PASSWORD)." -ForegroundColor Red
      Write-Host "             Nothing else is started, because a run would fetch" -ForegroundColor Red
      Write-Host "             every page and then extract nothing." -ForegroundColor Red
      Write-Host "             Fix: powershell -File scripts\dev.ps1 -Stop, then start" -ForegroundColor Red
      Write-Host "             opencode serve with OPENCODE_SERVER_PASSWORD set to that" -ForegroundColor Red
      Write-Host "             same value (scripts\llm.ps1 does it for you)." -ForegroundColor Red
      Get-Content "$LogDir\llm.log" -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object { "             $_" }
      exit 1
    }
    Write-Host "  llm      : up on $LlmPort, auth verified against .env"
  } else {
    Write-Host "  llm      : FAILED to start - see $LogDir\llm.log" -ForegroundColor Red
    Get-Content "$LogDir\llm.log" -Tail 5 -ErrorAction SilentlyContinue | ForEach-Object { "             $_" }
    exit 1
  }
}

# --- 2. the API --------------------------------------------------------------
$api = Get-Listener $ApiPort
if ($api) {
  Write-Host "  api      : already listening on $ApiPort (pid $api) - reusing it"
} else {
  $py = "python"
  $venvPy = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\python.exe"
  if (Test-Path $venvPy) { $py = $venvPy }
  # The .env path is relative to the working directory, so this has to run
  # from the repo root with --app-dir backend. Starting it anywhere else
  # silently loses ALLOW_UNAUTHENTICATED and every route returns 503.
  Start-Process -FilePath $py `
    -ArgumentList "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
                  "--host", "127.0.0.1", "--port", "$ApiPort" `
    -WorkingDirectory $Root -WindowStyle Hidden `
    -RedirectStandardOutput "$LogDir\api.out.log" `
    -RedirectStandardError "$LogDir\api.err.log"
  if (Wait-Http "http://127.0.0.1:$ApiPort/api/health" 45) {
    $persist = (Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/health" -TimeoutSec 20).data.persistence
    Write-Host "  api      : up on $ApiPort ($persist.adapter)"
    if ($persist.adapter -ne "postgres") {
      Write-Host "             WARNING: persistence is '$($persist.detail)'" -ForegroundColor Yellow
    }
  } else {
    Write-Host "  api      : FAILED - see $LogDir\api.err.log" -ForegroundColor Red
    Get-Content "$LogDir\api.err.log" -Tail 6 -ErrorAction SilentlyContinue | ForEach-Object { "             $_" }
    exit 1
  }
}

# --- 3. the frontend ---------------------------------------------------------
$web = Get-Listener $WebPort
if ($web) {
  Write-Host "  frontend : already listening on $WebPort (pid $web) - reusing it"
} else {
  Start-Process -FilePath "npm.cmd" -ArgumentList "run", "preview" `
    -WorkingDirectory (Join-Path $Root "frontend") -WindowStyle Hidden `
    -RedirectStandardOutput "$LogDir\web.log" -RedirectStandardError "$LogDir\web.err.log"
  if (Wait-Http "http://localhost:$WebPort" 40) {
    Write-Host "  frontend : up on $WebPort  ->  http://localhost:$WebPort"
  } else {
    Write-Host "  frontend : FAILED - see $LogDir\web.err.log" -ForegroundColor Red
    exit 1
  }
}

Write-Host ""
Write-Host "  run the checks:  cd frontend; npm run e2e" -ForegroundColor DarkGray
Write-Host "  stop everything: powershell -File scripts\dev.ps1 -Stop" -ForegroundColor DarkGray
