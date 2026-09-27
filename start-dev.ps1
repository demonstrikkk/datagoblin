# DATAGOBLIN local dev launcher (Windows). Run from the REPO ROOT:
#   powershell -ExecutionPolicy Bypass -File .\start-dev.ps1
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
Start-Process powershell -ArgumentList @(
  '-NoExit', '-Command',
  "cd '$root'; `$env:PYTHONPATH='$root\backend'; python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
) -WorkingDirectory $root
Start-Process powershell -ArgumentList @(
  '-NoExit', '-Command',
  "cd '$root\frontend'; npm run dev -- --port 5173 --strictPort"
) -WorkingDirectory "$root\frontend"
Write-Output 'API    http://127.0.0.1:8000/api/health'
Write-Output 'Free   http://127.0.0.1:8000/api/models/free'
Write-Output 'Studio http://localhost:5173/intel'
