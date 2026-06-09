param (
    [Parameter(Mandatory=$true)]
    [string]$TaskDir,

    [Parameter(Mandatory=$false)]
    [string]$OutFile
)

# 确保使用的是绝对路径
$TaskDir = Resolve-Path $TaskDir | Select-Object -ExpandProperty Path

# 验证任务目录是否存在
if (-not (Test-Path $TaskDir -PathType Container)) {
    Write-Error "任务目录不存在: $TaskDir"
    exit 1
}

$taskFolderName = Split-Path $TaskDir -Leaf

# 如果没有提供输出文件，自动在 outputs 目录下生成一个文件名
if ([string]::IsNullOrWhiteSpace($OutFile)) {
    $parentDir = Split-Path $TaskDir -Parent
    $OutFile = Join-Path $parentDir "merged_archive_$($taskFolderName)_with_common.zip"
} else {
    $OutFile = [System.IO.Path]::GetFullPath($OutFile)
}

# 从 manifest.json 中读取 common_task_id
$manifestPath = Join-Path $TaskDir "manifest.json"
if (-not (Test-Path $manifestPath)) {
    Write-Error "在任务目录中找不到 manifest.json: $TaskDir"
    exit 1
}

$manifestContent = Get-Content $manifestPath -Raw | ConvertFrom-Json
$commonTaskId = $manifestContent.common_task_id

if ([string]::IsNullOrWhiteSpace($commonTaskId)) {
    Write-Error "在 manifest.json 中未找到 common_task_id 字段。"
    exit 1
}

# 构建 common 目录的路径
$parentDir = Split-Path $TaskDir -Parent
$commonDir = Join-Path $parentDir "__common__\$commonTaskId"

if (-not (Test-Path $commonDir -PathType Container)) {
    Write-Error "对应的 Common 目录不存在: $commonDir"
    exit 1
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "打包任务目录: $TaskDir"
Write-Host "关联通用目录: $commonDir"
Write-Host "输出压缩包:   $OutFile"
Write-Host "========================================" -ForegroundColor Cyan

# 创建临时工作目录
$temp = Join-Path $parentDir "temp_zip_staging_$(New-Guid)"

if (Test-Path $OutFile) { Remove-Item -Path $OutFile -Force }
if (Test-Path $temp) { Remove-Item -Path $temp -Recurse -Force }
New-Item -ItemType Directory -Path $temp | Out-Null

$paths = @($TaskDir, $commonDir)

Write-Host "正在复制文件并过滤音视频、图片和 v1 目录..."
foreach ($source in $paths) {
    $folderName = Split-Path $source -Leaf
    $tempTarget = Join-Path $temp $folderName
    New-Item -ItemType Directory -Path $tempTarget | Out-Null
    
    # 过滤规则：过滤所有音视频、图片源文件，并过滤 v1 目录
    # 将 robocopy 的输出丢弃，保持终端清爽
    robocopy $source $tempTarget /S /XF *.mp4 *.avi *.mkv *.mov *.wmv *.flv *.webm *.m4v *.mp3 *.wav *.aac *.flac *.ogg *.wma *.m4a *.ts *.jpg *.jpeg *.png *.gif *.bmp *.webp /XD v1 | Out-Null
}

if (Test-Path "$temp\*") {
    Write-Host "正在打包为 ZIP 文件，请稍候..."
    Compress-Archive -Path "$temp\*" -DestinationPath $OutFile
    Write-Host "成功！压缩包已生成: $OutFile" -ForegroundColor Green
} else {
    Write-Host "警告: 过滤后没有剩下任何可以打包的文件。" -ForegroundColor Yellow
}

# 清理临时目录
Remove-Item -Path $temp -Recurse -Force
