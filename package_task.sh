#!/bin/bash
set -e

TASK_DIR="$1"
OUT_FILE="$2"

if [ -z "$TASK_DIR" ]; then
    echo "用法: ./package_task.sh <任务目录> [输出文件路径]"
    exit 1
fi

# 将目标目录转为绝对路径
if [[ "$TASK_DIR" != /* ]]; then
    # Windows环境下如果是以驱动器号开头（如 E:/）不转，但在 Git Bash 等环境下通常会被处理为 /e/ 或仍然保留路径，这里尽量兼容常规的bash。
    # 为了保险起见，做个简单的判断
    if [[ ! "$TASK_DIR" =~ ^[A-Za-z]: ]]; then
        TASK_DIR="$(pwd)/$TASK_DIR"
    fi
fi

if [ ! -d "$TASK_DIR" ]; then
    echo "错误: 任务目录不存在: $TASK_DIR"
    exit 1
fi

# 获取目录名称和父目录
TASK_NAME=$(basename "$TASK_DIR")
PARENT_DIR=$(dirname "$TASK_DIR")

# 自动生成输出文件名
if [ -z "$OUT_FILE" ]; then
    OUT_FILE="${PARENT_DIR}/merged_archive_${TASK_NAME}_with_common.zip"
else
    # 尝试将输出文件转绝对路径
    if [[ "$OUT_FILE" != /* ]] && [[ ! "$OUT_FILE" =~ ^[A-Za-z]: ]]; then
        OUT_FILE="$(pwd)/$OUT_FILE"
    fi
fi

# 读取 manifest.json 并提取 common_task_id
MANIFEST_PATH="$TASK_DIR/manifest.json"
if [ ! -f "$MANIFEST_PATH" ]; then
    echo "错误: 找不到 manifest.json 文件: $MANIFEST_PATH"
    exit 1
fi

# 优先使用 jq 解析，如果没有 jq，使用 grep 和 awk 作为兼容后备
if command -v jq >/dev/null 2>&1; then
    COMMON_TASK_ID=$(jq -r '.common_task_id // empty' "$MANIFEST_PATH")
else
    COMMON_TASK_ID=$(grep -m 1 '"common_task_id"' "$MANIFEST_PATH" | awk -F'"' '{print $4}')
fi

if [ -z "$COMMON_TASK_ID" ]; then
    echo "错误: 在 manifest.json 中未找到 common_task_id 字段"
    exit 1
fi

# 拼接 common 目录
COMMON_DIR="${PARENT_DIR}/__common__/${COMMON_TASK_ID}"

if [ ! -d "$COMMON_DIR" ]; then
    echo "错误: 关联的 common 目录不存在: $COMMON_DIR"
    exit 1
fi

echo "========================================"
echo "打包任务目录: $TASK_DIR"
echo "关联通用目录: $COMMON_DIR"
echo "输出压缩包  : $OUT_FILE"
echo "========================================"

# 创建临时工作区
TEMP_DIR="${PARENT_DIR}/temp_zip_staging_$$"
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"
rm -f "$OUT_FILE"

echo "正在复制文件并过滤音视频、图片及 v1 目录..."

# 定义要排除的文件和文件夹，使用 rsync 同步过滤
EXCLUDES=(
    --exclude="*.mp4" --exclude="*.avi" --exclude="*.mkv" --exclude="*.mov"
    --exclude="*.wmv" --exclude="*.flv" --exclude="*.webm" --exclude="*.m4v"
    --exclude="*.mp3" --exclude="*.wav" --exclude="*.aac" --exclude="*.flac"
    --exclude="*.ogg" --exclude="*.wma" --exclude="*.m4a" --exclude="*.ts"
    --exclude="*.jpg" --exclude="*.jpeg" --exclude="*.png" --exclude="*.gif"
    --exclude="*.bmp" --exclude="*.webp" --exclude="v1/"
)

# 拷贝任务目录
mkdir -p "${TEMP_DIR}/${TASK_NAME}"
tar -cf - "${EXCLUDES[@]}" -C "$TASK_DIR" . | tar -xf - -C "${TEMP_DIR}/${TASK_NAME}"

# 拷贝 common 目录
COMMON_NAME=$(basename "$COMMON_DIR")
mkdir -p "${TEMP_DIR}/${COMMON_NAME}"
tar -cf - "${EXCLUDES[@]}" -C "$COMMON_DIR" . | tar -xf - -C "${TEMP_DIR}/${COMMON_NAME}"

echo "正在生成 ZIP 文件，请稍候..."
# 进入临时目录并打包，保证 zip 内部的目录层级正确
(
    cd "$TEMP_DIR"
    zip -r -q "$OUT_FILE" ./*
)

echo "清理临时文件..."
rm -rf "$TEMP_DIR"

echo -e "\n成功！压缩包已生成: $OUT_FILE"
