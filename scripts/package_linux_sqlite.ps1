param(
    [string]$OutputDir = "dist",
    [string]$PackageName = "legal-demo-sqlite-minimal",
    [switch]$IncludeDataArchive,
    [switch]$NoTarGz,
    [switch]$NoZip
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $scriptDir "..")
$rootPath = $root.Path

if (-not (Test-Path (Join-Path $rootPath "app/main.py"))) {
    throw "This script must be run from the project checkout. app/main.py was not found."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputPath = Join-Path $rootPath $OutputDir
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) "legal-demo-package-$timestamp"
$stageApp = Join-Path $stageRoot "legal-demo"
if (Test-Path $stageRoot) {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stageApp | Out-Null

function Copy-IfExists {
    param([string]$RelativePath)
    $source = Join-Path $rootPath $RelativePath
    if (Test-Path $source) {
        $target = Join-Path $stageApp $RelativePath
        $targetParent = Split-Path -Parent $target
        if ($targetParent -and -not (Test-Path $targetParent)) {
            New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
    }
}

$requiredPaths = @(
    ".dockerignore",
    ".env.clean.example",
    ".env.example",
    ".env.sqlite.example",
    "app",
    "canlii_ingest.py",
    "CLEAN_PACKAGE_README.md",
    "DEPLOY.md",
    "DEPLOY_SQLITE_LINUX.md",
    "docker-compose.yml",
    "Dockerfile",
    "environment-linux-sqlite.yml",
    "export_rag_data.py",
    "llm_healthcheck.py",
    "rag_manage.py",
    "README.md",
    "requirements-local-embedding.txt",
    "requirements.txt",
    "scripts/deploy_linux_sqlite.sh",
    "scripts/package_linux_sqlite.ps1",
    "sql",
    "data"
)

foreach ($path in $requiredPaths) {
    Copy-IfExists $path
}

if ($IncludeDataArchive) {
    Copy-IfExists "data_archive"
}

$removePatterns = @(
    ".env",
    ".env.production",
    ".git",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "tmp",
    "dist",
    "*.log",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.pyc",
    "*.pyo"
)

foreach ($pattern in $removePatterns) {
    Get-ChildItem -LiteralPath $stageApp -Recurse -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like $pattern } |
        Sort-Object FullName -Descending |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
}

$createdPackages = @()

if (-not $NoTarGz) {
    $packagePath = Join-Path $outputPath "$PackageName-$timestamp.tar.gz"
    tar -czf $packagePath -C $stageRoot "legal-demo"
    $createdPackages += $packagePath
}

if (-not $NoZip) {
    $zipPath = Join-Path $outputPath "$PackageName-$timestamp.zip"
    if (Test-Path $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -LiteralPath $stageApp -DestinationPath $zipPath -CompressionLevel Optimal
    $createdPackages += $zipPath
}

Remove-Item -LiteralPath $stageRoot -Recurse -Force

Write-Host "Package created:"
foreach ($createdPackage in $createdPackages) {
    Write-Host $createdPackage
}
