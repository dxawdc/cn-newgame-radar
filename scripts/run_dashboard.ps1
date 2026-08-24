$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
Set-Location $projectRoot
python -m uvicorn newgame_monitor.webapp:app --host 127.0.0.1 --port 8765
