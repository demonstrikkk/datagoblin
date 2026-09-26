# DATAGOBLIN local dev launcher (Windows). Run from the REPO ROOT:
#   powershell -ExecutionPolicy Bypass -File .\start-dev.ps1
# Opens two consoles: FastAPI :8000 and Vite Studio :5173. Ctrl+C each to stop.
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
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
