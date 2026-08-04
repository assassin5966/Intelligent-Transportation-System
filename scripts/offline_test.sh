#!/bin/bash
#
# 离线视频测试 - 使用 video_processor 处理本地视频文件
#
# 用法:
#   ./offline_test.sh data/车辆识别20s.mp4                          # 车辆视频(默认配置)
#   ./offline_test.sh data/人流识别20s.mp4 --camera-type person      # 人流视频
#   ./offline_test.sh data/test_50f.mp4 --count-only enter           # 单向计数
#   ./offline_test.sh data/人流识别20s.mp4 --camera-type person --frame_skip 1  # 逐帧处理
#
# 可选参数 (透传给 video_processor.py):
#   --line "x1,y1,x2,y2"       计数线 (归一化0-1, 默认 0.1,0.75,0.9,0.75 水平线)
#   --anchor "x,y"             内侧锚点 (归一化0-1, 默认 0.5,0.9 下方)
#   --count-only enter|exit    单向计数模式
#   --camera-type vehicle|person  摄像头类型: vehicle=机动车, person=人流(含非机动车)
#   --frame_skip N             跳帧间隔 (1=逐帧, 5=每5帧处理1帧, 默认5)
#   --no-annotated             不生成标注视频
#

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if [ $# -lt 1 ]; then
    echo "用法: $0 <视频文件> [video_processor 参数...]"
    echo ""
    echo "示例:"
    echo "  $0 data/车辆识别20s.mp4"
    echo "  $0 data/人流识别20s.mp4 --camera-type person"
    echo "  $0 data/人流识别20s.mp4 --camera-type person --frame_skip 1"
    echo "  $0 data/test_50f.mp4 --count-only enter"
    exit 1
fi

VIDEO_FILE="$1"
shift
CAMERA_ID="${CAMERA_ID:-OFFLINE_TEST}"

# 默认计数线: 水平线在下方1/4处, 锚点在下方
DEFAULT_LINE="0.1,0.75,0.9,0.75"
DEFAULT_ANCHOR="0.5,0.9"

# 检查是否传了 --line, 没传则用默认值
HAS_LINE=false
for arg in "$@"; do
    if [ "$arg" = "--line" ]; then
        HAS_LINE=true
        break
    fi
done

if [ "$HAS_LINE" = false ]; then
    set -- --line "$DEFAULT_LINE" --anchor "$DEFAULT_ANCHOR" "$@"
fi

mkdir -p output

echo "========================================================"
echo "  离线视频测试 (Docker)"
echo "========================================================"
echo "视频文件: $VIDEO_FILE"
echo "摄像头ID: $CAMERA_ID"
echo "输出目录: $(pwd)/output"
echo "========================================================"
echo ""

docker run --rm \
    -v "$PROJECT_DIR/data:/data/video:ro" \
    -v "$PROJECT_DIR/output:/data/output" \
    -v "$PROJECT_DIR/models:/app/models:ro" \
    -v "$PROJECT_DIR/configs:/app/configs:ro" \
    -v "$PROJECT_DIR/tool:/app/tool:ro" \
    -v "$PROJECT_DIR/app:/app/app:ro" \
    -e PYTHONPATH=/app \
    smart-city-platform:latest \
    python /app/tool/video_processor.py \
    --video "/data/video/$(basename "$VIDEO_FILE")" \
    --output /data/output \
    --camera_id "$CAMERA_ID" \
    "$@"
