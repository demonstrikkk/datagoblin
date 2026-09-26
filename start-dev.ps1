# DATAGOBLIN local dev launcher (Windows). Run from the REPO ROOT:
#   powershell -ExecutionPolicy Bypass -File .\start-dev.ps1
# Opens consoles: opencode primary LLM :4096, FastAPI :8000, Vite Studio :5173.
# Ctrl+C each to stop. The opencode rung is the primary generator (local model,
# no cloud quota); without it the chain falls back to Groq -> Gemini.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$openCodeExe = "$env:APPDATA\npm\node_modules\opencode-ai\bin\opencode.exe"
if (Test-Path $openCodeExe) {
  Start-Process powershell -ArgumentList @(
    '-NoExit', '-Command',
    "cd '$root'; & '$openCodeExe' serve --port 4096 --hostname 127.0.0.1"
  ) -WorkingDirectory $root
  Write-Output 'OpenCode http://127.0.0.1:4096 (primary generator)'
} else {
  Write-Output 'OpenCode binary not found — chain will use Groq -> Gemini fallback.'
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
Write-Output 'Studio http://localhost:5173/'
