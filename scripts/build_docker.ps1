param(
    [string]$ImageName = "automatic-editing",
    [string]$Tag = "latest",
    [string]$CondaEnv = "comfy_5090_313_auto",
    [switch]$NoCache,
    [switch]$SaveTar,
    [string]$TarPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DockerDir = Join-Path $RepoRoot "docker"
$BuildInfoDir = Join-Path $DockerDir "build-info"
$ImageRef = "${ImageName}:${Tag}"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Command not found: $Name. Install/start Docker Desktop or add it to PATH."
    }
}

Set-Location $RepoRoot

Write-Step "Checking Docker"
Assert-Command "docker"
docker version | Out-Host

Write-Step "Writing environment snapshot"
New-Item -ItemType Directory -Force -Path $BuildInfoDir | Out-Null

$createdAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"
$contextBytes = (Get-ChildItem -Recurse -Force -File docker,models,tts_ref | Measure-Object -Property Length -Sum).Sum

@"
created_at=$createdAt
repo_root=$RepoRoot
image=$ImageRef
conda_env=$CondaEnv
context_size_bytes=$contextBytes
"@ | Set-Content -Path (Join-Path $BuildInfoDir "build.txt") -Encoding UTF8

if (Get-Command conda -ErrorAction SilentlyContinue) {
    try {
        conda env export -n $CondaEnv --no-builds | Set-Content -Path (Join-Path $BuildInfoDir "conda-env-export.yml") -Encoding UTF8
        conda run -n $CondaEnv python -m pip freeze | Set-Content -Path (Join-Path $BuildInfoDir "pip-freeze.txt") -Encoding UTF8
        Write-Host "Wrote docker/build-info/conda-env-export.yml and pip-freeze.txt"
    }
    catch {
        Write-Warning "Failed to export conda env; continuing Docker build. Error: $($_.Exception.Message)"
    }
}
else {
    Write-Warning "conda was not found; skipping environment snapshot export."
}

Write-Step "Building image $ImageRef"
$buildArgs = @("build", "-f", "docker/Dockerfile", "-t", $ImageRef)
if ($NoCache) {
    $buildArgs += "--no-cache"
}
$buildArgs += "."
& docker @buildArgs

Write-Step "Image build finished"
docker image ls $ImageName | Out-Host

if ($SaveTar) {
    if (-not $TarPath) {
        $safeTag = $Tag -replace "[^a-zA-Z0-9_.-]", "_"
        $TarPath = Join-Path $RepoRoot "${ImageName}-${safeTag}.tar"
    }
    Write-Step "Saving image to $TarPath"
    docker save -o $TarPath $ImageRef
    Write-Host "Saved: $TarPath" -ForegroundColor Green
}

Write-Host ""
Write-Host "Run example:" -ForegroundColor Green
Write-Host ".\scripts\run_docker.ps1 -ImageName $ImageName -Tag $Tag -CodePath `"$RepoRoot`""
Write-Host ""
if ($SaveTar) {
    Write-Host "For offline deployment, copy the tar file to the target machine and run:"
    Write-Host "docker load -i `"$TarPath`""
}
else {
    Write-Host "For an offline deployment tar, rebuild with -SaveTar:"
    Write-Host ".\scripts\build_docker.ps1 -ImageName $ImageName -Tag $Tag -SaveTar"
}
