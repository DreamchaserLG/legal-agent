[CmdletBinding()]
param(
    [ValidateRange(1, 4096)]
    [int]$BatchLimit = 512,
    [ValidateRange(1, 128)]
    [int]$EmbeddingBatchSize = 32,
    [ValidateRange(0, 255)]
    [int]$CpuCore = 0,
    [ValidateRange(30, 3600)]
    [int]$WaitSeconds = 1800
)

& (Join-Path $PSScriptRoot 'switch_vectorization_worker.ps1') `
    -Device 'cuda' `
    -BatchLimit $BatchLimit `
    -EmbeddingBatchSize $EmbeddingBatchSize `
    -CpuCore $CpuCore `
    -WaitSeconds $WaitSeconds
