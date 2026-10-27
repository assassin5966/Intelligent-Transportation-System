#!/bin/bash
#
# 视频切分 - 从长视频中截取指定时长片段
#
# 用法:
#   ./cut_video.sh data/人流识别.mp4 20           # 截取前20秒 -> data/人流识别20s.mp4
#   ./cut_video.sh data/人流识别.mp4 20 人流识别20s  # 指定输出文件名(不带扩展名)
#

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ $# -lt 2 ]; then
    echo "用法: $0 <输入视频> <秒数> [输出文件名(不带扩展名)]"
    echo "示例: $0 data/人流识别.mp4 20"
    echo "      $0 data/人流识别.mp4 20 人流识别20s"
    exit 1
fi

INPUT_VIDEO="$1"
DURATION="$2"

# 输出文件名: 参数3 或 原文件名+时长s
if [ -n "$3" ]; then
    OUTPUT_NAME="$3"
else
    BASENAME=$(basename "$INPUT_VIDEO" .mp4)
    OUTPUT_NAME="${BASENAME}${DURATION}s"
fi

OUTPUT_VIDEO="data/${OUTPUT_NAME}.mp4"

echo "=== 视频切分 ==="
echo "输入: $INPUT_VIDEO"
echo "时长: ${DURATION}秒"
echo "输出: $OUTPUT_VIDEO"
echo ""

docker run --rm \
    -v "$PROJECT_DIR/data:/data/video" \
    smart-city-platform:latest \
    ffmpeg -i "/data/video/$(basename "$INPUT_VIDEO")" \
    -t "$DURATION" \
    -c:v libx264 -preset ultrafast \
    "/data/video/$OUTPUT_NAME.mp4" \
    -y

echo ""
echo "=== 切分完成 ==="
echo "输出文件: $OUTPUT_VIDEO"
