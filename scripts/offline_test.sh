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
#   # 详细计数诊断: 逐事件打印 + 输出 <视频>_trace.json (逐目标跨线生命周期)
#   ./offline_test.sh data/车辆识别20s.mp4 --verbose
#
#   # 后台运行 (nohup), 详细日志落 logs/offline_<视频>_<时间>.log, 可随时拉起查看
#   ./offline_test.sh data/车辆识别20s.mp4 --verbose --background
#   tail -f logs/offline_车辆识别20s_*.log
#
# 可选参数 (透传给 video_processor.py):
#   --line "x1,y1,x2,y2"       计数线 (归一化0-1, 默认 0.1,0.75,0.9,0.75 水平线)
#   --anchor "x,y"             内侧锚点 (归一化0-1, 默认 0.5,0.9 下方)
#   --count-only enter|exit    单向计数模式
#   --camera-type vehicle|person  摄像头类型: vehicle=机动车, person=人流(含非机动车)
#   --frame_skip N             跳帧间隔 (1=逐帧, 5=每5帧处理1帧, 默认5)
#   --no-annotated             不生成标注视频
#   --verbose                  详细计数诊断 (逐事件打印 + trace.json)
#
# 脚本级参数 (不传给 video_processor.py):
#   --background               后台运行, 输出重定向到 logs/ 下的日志文件
#

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# 脚本级参数 --background (仅识别首个参数, 避免与 video_processor 参数混淆)
BACKGROUND=false
if [ "${1:-}" = "--background" ]; then
    BACKGROUND=true
    shift
fi

if [ $# -lt 1 ]; then
    echo "用法: $0 [--background] <视频文件> [video_processor 参数...]"
    echo ""
    echo "示例:"
    echo "  $0 data/车辆识别20s.mp4"
    echo "  $0 data/人流识别20s.mp4 --camera-type person"
    echo "  $0 data/人流识别20s.mp4 --camera-type person --frame_skip 1"
    echo "  $0 data/test_50f.mp4 --count-only enter"
    echo "  $0 data/车辆识别20s.mp4 --verbose --background   # 后台运行并落日志"
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

mkdir -p output logs

VIDEO_NAME="$(basename "$VIDEO_FILE")"
LOG_FILE="$PROJECT_DIR/logs/offline_${VIDEO_NAME%.*}_$(date +%Y%m%d_%H%M%S).log"
CONTAINER_NAME="smart-city-offline-$(date +%s)"

# 组装 docker run 参数 (stdout/stderr 统一由调用方重定向到日志文件)
DOCKER_ARGS=(
    --rm
    --name "$CONTAINER_NAME"
    -v "$PROJECT_DIR/data:/data/video:ro"
    -v "$PROJECT_DIR/output:/data/output"
    -v "$PROJECT_DIR/logs:/app/logs"
    -v "$PROJECT_DIR/models:/app/models:ro"
    -v "$PROJECT_DIR/configs:/app/configs:ro"
    -v "$PROJECT_DIR/tool:/app/tool:ro"
    -v "$PROJECT_DIR/app:/app/app:ro"
    -e PYTHONPATH=/app
    smart-city-platform:latest
    python /app/tool/video_processor.py
    --video "/data/video/$VIDEO_NAME"
    --output /data/output
    --camera_id "$CAMERA_ID"
    "$@"
)

echo "========================================================"
echo "  离线视频测试 (Docker)"
echo "========================================================"
echo "视频文件: $VIDEO_FILE"
echo "摄像头ID: $CAMERA_ID"
echo "输出目录: $(pwd)/output"
echo "日志文件: $LOG_FILE"
echo "运行模式: $([ "$BACKGROUND" = true ] && echo "后台 (nohup)" || echo "前台")"
echo "========================================================"
echo ""

if [ "$BACKGROUND" = true ]; then
    # 后台运行: 全部输出 (含 --verbose 逐事件明细/进度) 重定向到日志文件, 便于之后拉起查看
    nohup docker run "${DOCKER_ARGS[@]}" >> "$LOG_FILE" 2>&1 &
    RUN_PID=$!
    echo "已在后台启动 (PID: $RUN_PID, 容器: $CONTAINER_NAME)"
    echo "查看实时日志: tail -f \"$LOG_FILE\""
    echo "等待完成:     wait $RUN_PID  或   docker wait $CONTAINER_NAME"
    echo "停止:         docker stop $CONTAINER_NAME"
else
    # 前台运行: 实时输出同时落日志文件 (tee)
    docker run "${DOCKER_ARGS[@]}" 2>&1 | tee "$LOG_FILE"
fi
