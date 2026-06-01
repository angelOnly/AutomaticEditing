param(
    [string]$ImageName = "automatic-editing",
    [string]$Tag = "latest",
    [string]$ContainerName = "automatic-editing",
    [int]$Port = 7860,
    [string]$CodePath = "",
    [string]$VideosPath = "",
    [string]$OutputsPath = "",
    [switch]$NoGpu
)

$ErrorActionPreference = "Stop"
$ImageRef = "${ImageName}:${Tag}"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $CodePath) {
    $CodePath = $RepoRoot
}
if (-not $VideosPath) {
    $VideosPath = Join-Path $CodePath "videos"
}
if (-not $OutputsPath) {
    $OutputsPath = Join-Path $CodePath "outputs"
}

New-Item -ItemType Directory -Force -Path $VideosPath | Out-Null
New-Item -ItemType Directory -Force -Path $OutputsPath | Out-Null

$CodePath = (Resolve-Path $CodePath).Path
$VideosPath = (Resolve-Path $VideosPath).Path
$OutputsPath = (Resolve-Path $OutputsPath).Path

$args = @(
    "run",
    "--rm",
    "-p", "${Port}:7860",
    "--name", $ContainerName,
    "-v", "${CodePath}:/workspace",
    "-v", "${VideosPath}:/workspace/videos",
    "-v", "${OutputsPath}:/workspace/outputs"
)
if (-not $NoGpu) {
    $args += @("--gpus", "all")
}
$args += $ImageRef

Write-Host "Starting: docker $($args -join ' ')" -ForegroundColor Cyan
& docker @args
