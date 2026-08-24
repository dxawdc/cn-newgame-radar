$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'
Set-Location $projectRoot
python -m newgame_monitor.cli --sources huawei-cache xiaomi
