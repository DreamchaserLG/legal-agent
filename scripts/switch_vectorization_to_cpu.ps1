[CmdletBinding()]
param(
    [ValidateRange(1, 64)]
    [int]$BatchLimit = 2,
    [ValidateRange(0, 255)]
    [int]$CpuCore = 0,
    [ValidateRange(1, 64)]
    [int]$CpuCores = 4,
    [ValidateRange(30, 3600)]
    [int]$WaitSeconds = 1800
)

& (Join-Path $PSScriptRoot 'switch_vectorization_worker.ps1') `
    -Device 'cpu' `
    -BatchLimit $BatchLimit `
    -EmbeddingBatchSize $BatchLimit `
    -CpuCore $CpuCore `
    -CpuCores $CpuCores `
    -WaitSeconds $WaitSeconds
