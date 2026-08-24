$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $projectRoot 'src'

if (-not $env:NEWGAME_ADB) {
    throw '请先设置 NEWGAME_ADB，指向本机 adb 可执行文件'
}
if (-not $env:NEWGAME_ADB_SERIAL) {
    $env:NEWGAME_ADB_SERIAL = '127.0.0.1:16384'
}

Set-Location $projectRoot
python -m newgame_monitor.cli `
    --sources taptap huawei-cache honor-ui oppo-ui `
    --db data/newgame_monitor.db `
    --raw-dir raw `
    --icon-dir data/icons `
    --screenshot-dir data/screenshots

if ($LASTEXITCODE -ne 0) {
    throw "模拟器渠道采集失败，退出码：$LASTEXITCODE"
}
