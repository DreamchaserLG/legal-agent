[CmdletBinding()]
param(
    [ValidateSet('cpu', 'cuda')]
    [string]$Device,
    [ValidateRange(1, 4096)]
    [int]$BatchLimit,
    [ValidateRange(1, 128)]
    [int]$EmbeddingBatchSize = 32,
    [ValidateRange(0, 255)]
    [int]$CpuCore = 0,
    [ValidateRange(1, 64)]
    [int]$CpuCores = 4,
    [ValidateRange(30, 3600)]
    [int]$WaitSeconds = 1800
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logsDirectory = Join-Path $projectRoot 'logs'
$stateFile = Join-Path $logsDirectory 'idle_vectorization.pid'
$requestFile = Join-Path $logsDirectory 'vectorization_switch.request'
New-Item -ItemType Directory -Path $logsDirectory -Force | Out-Null

$previousWorkerId = $null
if (Test-Path -LiteralPath $stateFile) {
    $text = (Get-Content -LiteralPath $stateFile -Raw).Trim()
    if ($text -match '^\d+$') {
        $previousWorkerId = [int]$text
    }
}

if ($previousWorkerId -and (Get-Process -Id $previousWorkerId -ErrorAction SilentlyContinue)) {
    Set-Content -LiteralPath $requestFile -Value "requested_at=$([DateTime]::UtcNow.ToString('o')); target=$Device" -Encoding ascii
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while (Get-Process -Id $previousWorkerId -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $deadline) {
            throw "Timed out waiting for worker $previousWorkerId to finish its current batch. It was not terminated."
        }
        Start-Sleep -Seconds 1
    }
}

if (Test-Path -LiteralPath $stateFile) {
    Remove-Item -LiteralPath $stateFile -Force
}
if (Test-Path -LiteralPath $requestFile) {
    Remove-Item -LiteralPath $requestFile -Force
}

$env:VECTOR_WORKER_DEVICE = $Device
$env:VECTOR_WORKER_EMBEDDING_BATCH_SIZE = "$EmbeddingBatchSize"
$env:VECTOR_WORKER_CPU_THREADS = "$CpuCores"
$env:VECTOR_WORKER_IGNORE_GPU_GATE = if ($Device -eq 'cuda') { 'true' } else { 'false' }
$pythonPath = (Get-Command python -ErrorAction Stop).Source
$workerScript = Join-Path $PSScriptRoot 'run_idle_vectorization.py'
$arguments = @(
    $workerScript,
    '--batch-limit', "$BatchLimit",
    '--cpu-core', "$CpuCore",
    '--cpu-cores', "$CpuCores",
    '--log-path', (Join-Path $logsDirectory 'idle_vectorization.jsonl')
)
if ($Device -eq 'cuda') {
    $arguments += @('--min-idle-seconds', '0', '--poll-seconds', '1')
}
$worker = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
$worker.Id | Set-Content -LiteralPath $stateFile -Encoding ascii
Write-Output "Switched vectorization to $Device after a completed batch. PID: $($worker.Id). HNSW is not built here."
