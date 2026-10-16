# DATAGOBLIN local dev launcher (Windows). Run from the REPO ROOT:
#   powershell -ExecutionPolicy Bypass -File .\start-dev.ps1
#   powershell -ExecutionPolicy Bypass -File .\start-dev.ps1 -Watch
param([switch]$Watch)
# Opens consoles: opencode primary LLM :4096, FastAPI :8000, Vite Studio :5173.
# Ctrl+C each to stop. The opencode rung is the primary generator (local model,
# no cloud quota); without it the chain falls back to Groq -> Gemini.
#
# opencode is also the ONLY route to 9 of the 10 free Zen models (the rest return
# 403 FreeTierError on direct REST), so start it or /intel answers one model.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# The backend authenticates with Basic(opencode:<OPENCODE_PASSWORD>), but the
# server only enforces it when ITS OWN OPENCODE_SERVER_PASSWORD is set. Measured:
# a server started without it returns 200 for a correct password, a wrong
# password, and no password at all — so a password configured in .env while the
# server runs bare is silently doing nothing. Forward the same value so the two
# halves actually agree.
$ocPassword = ''
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
  $m = Select-String -Path $envFile -Pattern '^OPENCODE_PASSWORD=(.*)$' |
       Select-Object -First 1
  if ($m) { $ocPassword = $m.Matches[0].Groups[1].Value.Trim() }
}

$openCodeExe = "$env:APPDATA\npm\node_modules\opencode-ai\bin\opencode.exe"
if (Test-Path $openCodeExe) {
  $ocCmd = "cd '$root'; `$env:OPENCODE_SERVER_PASSWORD='$ocPassword'; " +
           "& '$openCodeExe' serve --port 4096 --hostname 127.0.0.1"
  Start-Process powershell -ArgumentList @('-NoExit', '-Command', $ocCmd) `
    -WorkingDirectory $root
  if ($ocPassword) {
    Write-Output 'OpenCode http://127.0.0.1:4096 (primary generator, Basic auth on)'
  } else {
    Write-Output 'OpenCode http://127.0.0.1:4096 (primary generator, NO AUTH: any local process can use it)'
  }
} else {
  Write-Output 'OpenCode binary not found - chain uses Groq -> Gemini, and 9 of 10 free models are unavailable.'
}

# Prefer the project venv. A bare `python` resolves to whatever is first on
# PATH, which is how uvicorn ends up running against a different interpreter
# than the one psycopg-pool was installed into.
$py = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

Start-Process powershell -ArgumentList @(
  '-NoExit', '-Command',
  "cd '$root'; `$env:PYTHONPATH='$root\backend'; & '$py' -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
) -WorkingDirectory $root
Start-Process powershell -ArgumentList @(
  '-NoExit', '-Command',
  "cd '$root\frontend'; npm run dev -- --port 5173 --strictPort"
) -WorkingDirectory "$root\frontend"

# The watcher. All three services were observed disappearing at once, mid-crawl,
# with no traceback and no Windows crash event — which is what a resource kill
# looks like from the outside. Nothing restarts a dead uvicorn on its own, so a
# single OOM during extraction silently ended the stack and the next request came
# back as a connection refused that looked like a frontend bug.
#
# It probes each port and restarts ONLY the one that is down. It acts on its own
# port and nothing else, which matters here: other Node processes on this box
# (unrelated dev servers) share the machine and must never be touched.
if (-not $Watch) {
  Write-Output 'Watcher off. Add -Watch to restart a service that dies.'
} else {
  Start-Job -Name 'dg-watch' -ScriptBlock {
    param($root, $py, $ocPassword, $openCodeExe)
    while ($true) {
      Start-Sleep -Seconds 15
      foreach ($svc in @(
        @{ n = 'API';    p = 8000; exe = $py;     kind = 'api' },
        @{ n = 'Studio'; p = 5173; exe = 'npm';    kind = 'web' },
        @{ n = 'OpenCode'; p = 4096; exe = $openCodeExe; kind = 'oc' }
      )) {
        $c = Get-NetTCPConnection -State Listen -LocalPort $svc.p `
             -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { continue }
        if ($svc.kind -eq 'api') {
          $env:PYTHONPATH = "$root\backend"
          Start-Process $svc.exe -ArgumentList @(
            '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000'
          ) -WorkingDirectory $root -WindowStyle Minimized
        } elseif ($svc.kind -eq 'web') {
          Start-Process powershell -ArgumentList @(
            '-NoExit', '-Command',
            "cd '$root\frontend'; npm run dev -- --port 5173 --strictPort"
          ) -WorkingDirectory "$root\frontend" -WindowStyle Minimized
        } elseif ($svc.kind -eq 'oc' -and (Test-Path $svc.exe)) {
          Start-Process powershell -ArgumentList @(
            '-NoExit', '-Command',
            "cd '$root'; `$env:OPENCODE_SERVER_PASSWORD='$ocPassword'; " +
            "& '$($svc.exe)' serve --port 4096 --hostname 127.0.0.1"
          ) -WorkingDirectory $root -WindowStyle Minimized
        }
        Write-Output "[watch] $($svc.n) (:$($svc.p)) was down - restarted"
      }
    }
  } -ArgumentList $root, $py, $ocPassword, $openCodeExe
  Write-Output 'Watcher on (15s). Stop it with: Stop-Job dg-watch'
}

# Wait for the ports to answer rather than reporting success and hoping. A
# launcher that prints URLs for services that never bound is worse than one that
# says which one failed.
$deadline = (Get-Date).AddSeconds(45)
$targets = @(
  @{ name = 'API    '; url = 'http://127.0.0.1:8000/api/health' },
  @{ name = 'Studio '; url = 'http://127.0.0.1:5173/' }
)
while ((Get-Date) -lt $deadline) {
  $pending = @($targets | Where-Object {
    try { $null = Invoke-WebRequest $_.url -UseBasicParsing -TimeoutSec 3; $false }
    catch { $true }
  })
  if ($pending.Count -eq 0) { break }
  Start-Sleep -Seconds 2
}
foreach ($t in $targets) {
  try {
    $null = Invoke-WebRequest $t.url -UseBasicParsing -TimeoutSec 5
    Write-Output "$($t.name) up   $($t.url)"
  } catch {
    Write-Output "$($t.name) DOWN  $($t.url)  <- see that window's output"
  }
}
# 127.0.0.1, not localhost: vite binds IPv4 explicitly, and a host that
# resolves localhost to ::1 first will fail to connect.
Write-Output 'Free   http://127.0.0.1:8000/api/models/free'
Write-Output 'Studio http://127.0.0.1:5173/intel'
