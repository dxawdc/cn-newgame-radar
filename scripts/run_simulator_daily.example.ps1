$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $projectRoot 'runtime\simulator-sync'
$logRoot = Join-Path $projectRoot 'logs\simulator'
$runId = Get-Date -Format 'yyyyMMdd-HHmmss'
$runRoot = Join-Path $runtimeRoot $runId
$logPath = Join-Path $logRoot "$runId.log"
$statusPath = Join-Path $logRoot 'latest.json'
$serverAlias = 'newgame-server'
$remoteRoot = '/opt/newgame-monitor/current'
$remoteState = '/var/lib/newgame-monitor'
$remoteBundle = "/tmp/newgame-simulator-$runId.tar.gz"
$remoteSnapshot = "/tmp/newgame-server-base-$runId.db"
$mumuLaunchTimeoutSeconds = 300
$mutexStatePath = Join-Path $logRoot 'mutex.json'
$mumuCli = $env:NEWGAME_MUMU_CLI
$adb = $env:NEWGAME_ADB
$adbSerial = '127.0.0.1:16384'
$mutex = [System.Threading.Mutex]::new($false, 'Global\NewGameSimulatorDaily')
$hasMutex = $false
$transcriptStarted = $false
$succeeded = $false
$runStatus = 'failed'
$channelResults = @()
$importResult = $null
$failureStage = '环境'
$failureMessage = $null
$startedAt = (Get-Date).ToString('o')

function Invoke-WithRetry {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Operation,
        [int]$Attempts = 3,
        [int]$DelaySeconds = 10,
        [string]$Name = '操作'
    )
    $lastError = $null
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            & $Operation
            return
        }
        catch {
            $lastError = $_
            Write-Warning "$Name 第 $attempt/$Attempts 次失败：$($_.Exception.Message)"
            if ($attempt -lt $Attempts) { Start-Sleep -Seconds $DelaySeconds }
        }
    }
    throw $lastError
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    # 子进程不依赖 SSH 自己退出；输出异步读取，避免管道写满死锁。
    $info = [System.Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $FilePath
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    if ([IO.Path]::GetFileNameWithoutExtension($FilePath) -in @('ssh', 'scp')) {
        foreach ($option in @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3')) {
            $info.ArgumentList.Add($option)
        }
    }
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(180000)) {
            $process.Kill($true)
            $process.WaitForExit()
            throw "$FilePath 超过180秒，已结束本地等待进程；远端托管任务可继续运行"
        }
        $output = $stdout.GetAwaiter().GetResult()
        $errorOutput = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "$FilePath 执行失败，退出码 $($process.ExitCode)：$errorOutput"
        }
        if ($errorOutput) { Write-Host $errorOutput }
        if ($output) { Write-Output $output }
    }
    finally {
        $process.Dispose()
    }
}

function Get-MuMuInfo {
    $output = & $mumuCli info -v 0 2>&1
    if ($LASTEXITCODE -ne 0) { return $null }
    try { return ($output -join "`n" | ConvertFrom-Json) } catch { return $null }
}

try {
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    try {
        $hasMutex = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
        Write-Warning '检测到上轮进程异常退出留下的 abandoned mutex，已接管锁'
    }
    if (-not $hasMutex) {
        if (Test-Path -LiteralPath $mutexStatePath) {
            try {
                $holder = Get-Content -LiteralPath $mutexStatePath -Raw | ConvertFrom-Json
                Write-Warning "已有一轮模拟器采集正在运行，本轮退出；持有进程 PID=$($holder.pid)，开始时间=$($holder.started_at)"
            }
            catch {
                Write-Warning '已有一轮模拟器采集正在运行，本轮退出；锁状态文件不可读'
            }
        }
        else {
            Write-Warning '已有一轮模拟器采集正在运行，本轮退出；未找到锁状态文件'
        }
        throw '已有一轮模拟器采集正在运行，本轮退出'
    }
    [ordered]@{ pid = $PID; run_id = $runId; started_at = $startedAt } |
        ConvertTo-Json | Set-Content -LiteralPath $mutexStatePath -Encoding utf8
    New-Item -ItemType Directory -Force -Path $runRoot | Out-Null
    Start-Transcript -LiteralPath $logPath -Force | Out-Null
    $transcriptStarted = $true
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 开始模拟器每日采集"

    foreach ($required in @($mumuCli, $adb)) {
        if (-not [System.IO.File]::Exists($required)) { throw "缺少运行组件：$required" }
    }
    $python = (Get-Command python -ErrorAction Stop).Source
    $scp = (Get-Command scp -ErrorAction Stop).Source
    $ssh = (Get-Command ssh -ErrorAction Stop).Source

    $info = Get-MuMuInfo
    if ($null -eq $info -or -not $info.is_android_started) {
        Write-Host 'MuMu 未启动，正在唤醒实例 0'
        $launchProcess = Start-Process -FilePath $mumuCli -ArgumentList @('control', '-v', '0', 'launch') -WindowStyle Hidden -PassThru
        $deadline = (Get-Date).AddSeconds($mumuLaunchTimeoutSeconds)
        do {
            Start-Sleep -Seconds 10
            $info = Get-MuMuInfo
            if ($launchProcess.HasExited -and $launchProcess.ExitCode -ne 0) {
                throw "MuMu 启动命令失败，退出码 $($launchProcess.ExitCode)"
            }
        } until (($null -ne $info -and $info.is_android_started) -or (Get-Date) -ge $deadline)
        if ($null -eq $info -or -not $info.is_android_started) {
            throw "MuMu 在 $mumuLaunchTimeoutSeconds 秒内未完成启动"
        }
    }
    Write-Host "MuMu 已就绪：实例 $($info.index)，ADB $($info.adb_host_ip):$($info.adb_port)"

    Invoke-WithRetry -Name 'ADB 连接' -Operation {
        & $adb connect $adbSerial | Out-Host
        $state = (& $adb -s $adbSerial get-state 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $state -ne 'device') { throw "ADB 状态异常：$state" }
    }

    $baseDb = Join-Path $runRoot 'server-base.db'
    $workDb = Join-Path $runRoot 'newgame_monitor.db'
    $rawDir = Join-Path $runRoot 'raw'
    $iconDir = Join-Path $runRoot 'icons'
    $bundle = Join-Path $runRoot "newgame-simulator-$runId.tar.gz"
    New-Item -ItemType Directory -Force -Path $rawDir, $iconDir | Out-Null

    $failureStage = '服务器快照'
    Invoke-WithRetry -Name '生成并下载服务器一致快照' -Operation {
        Invoke-External $ssh $serverAlias "/usr/bin/timeout 120 /opt/newgame-monitor/venv/bin/python $remoteRoot/scripts/sqlite_backup.py $remoteState/data/newgame_monitor.db $remoteSnapshot"
        Invoke-External $scp "$serverAlias`:$remoteSnapshot" $baseDb
    }
    Invoke-External $ssh $serverAlias "unlink $remoteSnapshot"
    $quickCheckProgram = 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); r=c.execute("PRAGMA quick_check").fetchone()[0]; c.close(); raise SystemExit(0 if r=="ok" else 2)'
    Invoke-External $python -c $quickCheckProgram $baseDb
    Copy-Item -LiteralPath $baseDb -Destination $workDb -Force
    $collectionSince = (Get-Date).ToString('o')

    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:NEWGAME_ADB = $adb
    $env:NEWGAME_ADB_SERIAL = $adbSerial
    $env:NEWGAME_REFRESH_HUAWEI = '1'
    $env:NEWGAME_ICON_DIR = $iconDir
    Set-Location $projectRoot
    Write-Host '开始采集 TapTap、九游及华为、荣耀、OPPO 本机补充渠道'
    $failureStage = '渠道采集/目录重建'
    & $python -m newgame_monitor.cli `
        --sources taptap 9game huawei-cache honor-ui oppo-ui `
        --detail-budget-seconds 1800 --detail-max-products 0 `
        --db $workDb --raw-dir $rawDir --icon-dir $iconDir 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "模拟器采集失败，退出码 $LASTEXITCODE" }

    Write-Host '生成增量同步包'
    $failureStage = '导出'
    Invoke-External $python -m newgame_monitor.simulator_sync export `
        --db $workDb --base-db $baseDb --raw-dir $rawDir --icon-dir $iconDir `
        --output $bundle --since $collectionSince

    $failureStage = '上传'
    Invoke-WithRetry -Name '上传增量同步包' -Operation {
        Invoke-External $scp $bundle "$serverAlias`:$remoteBundle"
    }
    $failureStage = '远端导入'
    $jobUnit = "newgame-monitor-import@$runId.service"
    Invoke-External $ssh $serverAlias "chown newgame-monitor:newgame-monitor $remoteBundle && chmod 640 $remoteBundle && systemctl start --no-block $jobUnit"
    $importDeadline = (Get-Date).AddMinutes(17)
    $job = $null
    do {
        Start-Sleep -Seconds 10
        try {
            $job = (Invoke-External $ssh $serverAlias "env PYTHONPATH=$remoteRoot/src /opt/newgame-monitor/venv/bin/python -m newgame_monitor.import_job status --run-id $runId") | ConvertFrom-Json
        }
        catch {
            Write-Warning "入库状态查询暂不可用：$($_.Exception.Message)"
            continue
        }
        if ($job.state -eq 'failed') { throw "远端入库服务失败：$($job | ConvertTo-Json -Depth 12 -Compress)" }
        if ($job.state -eq 'success') { break }
    } while ((Get-Date) -lt $importDeadline)
    if ($null -eq $job -or $job.state -ne 'success') {
        throw "远端入库观察超时，请核查 $jobUnit；不重复启动入库"
    }
    $importResult = $job.result
    Invoke-External $ssh $serverAlias "unlink $remoteBundle"

    Copy-Item -LiteralPath $workDb -Destination (Join-Path $projectRoot 'data\newgame_monitor.db') -Force
    $projectIconDir = Join-Path $projectRoot 'data\icons'
    New-Item -ItemType Directory -Force -Path $projectIconDir | Out-Null
    Get-ChildItem -LiteralPath $iconDir -Force | Copy-Item -Destination $projectIconDir -Recurse -Force
    $failureStage = '健康核验'
    $health = Invoke-RestMethod 'http://106.14.158.142/newgame/api/health' -TimeoutSec 30
    $requiredSources = @('taptap', 'huawei-cache', 'honor-ui', 'oppo-ui')
    $reportedSources = @($requiredSources + '9game')
    $latest = @($health.sources | Where-Object {
        $_.source -in $reportedSources -and $_.finished_at -and
        [DateTimeOffset]::Parse($_.finished_at) -ge [DateTimeOffset]::Parse($startedAt)
    })
    $requiredLatest = @($latest | Where-Object { $_.source -in $requiredSources })
    $publishableLatest = @($requiredLatest | Where-Object { $_.status -in @('success', 'degraded') })
    if ($publishableLatest.Count -eq 0) {
        throw '服务器健康检查未确认任一渠道成功发布'
    }
    $channelResults = $latest
    $failedLatest = @($requiredLatest | Where-Object { $_.status -notin @('success', 'degraded') })
    $runStatus = if ($failedLatest.Count -gt 0 -or $requiredLatest.Count -ne $requiredSources.Count) { 'partial' } else { 'success' }
    $succeeded = $true
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 模拟器采集与服务器同步完成：$runStatus"
}
catch {
    $failureMessage = $_.Exception.Message
    Write-Warning $_.Exception.Message
    throw
}
finally {
    $status = [ordered]@{
        run_id = $runId
        started_at = $startedAt
        finished_at = (Get-Date).ToString('o')
        status = $runStatus
        channels = $channelResults
        log = $logPath
        workspace = if ($succeeded) { $null } else { $runRoot }
        import_result = $importResult
        failure_stage = if ($succeeded) { $null } else { $failureStage }
        error = $failureMessage
    }
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    if ($hasMutex) {
        $status | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath "$statusPath.tmp" -Encoding utf8
        Move-Item -LiteralPath "$statusPath.tmp" -Destination $statusPath -Force
    }
    if ($succeeded -and [System.IO.Directory]::Exists($runRoot)) {
        $resolvedRun = [System.IO.Path]::GetFullPath($runRoot)
        $resolvedRuntime = [System.IO.Path]::GetFullPath($runtimeRoot)
        if (-not $resolvedRun.StartsWith($resolvedRuntime, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "拒绝清理运行目录之外的路径：$resolvedRun"
        }
        Remove-Item -LiteralPath $resolvedRun -Recurse -Force
    }
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
    if ($hasMutex -and (Test-Path -LiteralPath $mutexStatePath)) {
        try {
            $holder = Get-Content -LiteralPath $mutexStatePath -Raw | ConvertFrom-Json
            if ([int]$holder.pid -eq $PID) {
                Remove-Item -LiteralPath $mutexStatePath -Force -ErrorAction SilentlyContinue
            }
        }
        catch {
            Remove-Item -LiteralPath $mutexStatePath -Force -ErrorAction SilentlyContinue
        }
    }
    if ($hasMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
