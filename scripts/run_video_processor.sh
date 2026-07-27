#!/bin/bash
#
# 视频文件处理 - Docker 一键运行脚本
#
# 用法:
#   ./run_video_processor.sh                              # 使用默认参数处理
#   ./run_video_processor.sh --frame_skip 1               # 每帧都处理（慢但精确）
#   ./run_video_processor.sh --frame_skip 10              # 每10帧处理1帧（快）
#   ./run_video_processor.sh --no-annotated               # 不生成标注视频（更快）
#   ./run_video_processor.sh --line 0.5,0.1,0.5,0.9       # 自定义计数线 (归一化 x1,y1,x2,y2, 默认垂直线)
#


set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

VIDEO_FILE="output/test_50f.mp4"

FRAME_SKIP="${FRAME_SKIP:-5}"
CAMERA_ID="${CAMERA_ID:-CAM001}"

mkdir -p output

echo "========================================================"
echo "  智慧交管 - 视频文件处理器 (Docker)"
echo "========================================================"
echo "视频文件: $VIDEO_FILE"
echo "跳帧间隔: 每${FRAME_SKIP}帧处理1帧"
echo "摄像头ID: $CAMERA_ID"
echo "输出目录: $(pwd)/output"
echo "模型文件: models/yolo11n.pt"
echo "========================================================"
echo ""

docker compose run --rm \
    ai-processor \
    python /app/tool/video_processor.py \
    --video "/data/video/$(basename "$VIDEO_FILE")" \
    --output /data/output \
    --camera_id "$CAMERA_ID" \
    --frame_skip "$FRAME_SKIP" \
    "$@"