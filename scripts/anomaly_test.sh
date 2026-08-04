#!/bin/bash
#
# 视频异常识别端到端测试 (黑屏 + 花屏)
#
# 流程: 生成合成测试视频 (含正常/黑屏/花屏段) -> Docker 运行 video_processor -> 校验异常 JSON
#
# 用法:
#   ./anomaly_test.sh                    # 默认 (生成视频 + 逐帧处理 + 校验)
#   ./anomaly_test.sh --no-annotated     # 不生成标注视频 (更快, 仅校验 JSON)
#   SEGMENT=120 ./anomaly_test.sh        # 自定义每段帧数
#
# 前置: Docker 已启动且 smart-city-platform:latest 镜像已构建。
#       生成视频仅需宿主机 python3 + numpy + opencv (无需 YOLO)。

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

SEGMENT="${SEGMENT:-90}"
VIDEO_FILE="data/anomaly_test.mp4"
CAMERA_ID="${CAMERA_ID:-ANOMALY_TEST}"
OUTPUT_DIR="output"

echo "========================================================"
echo "  视频异常识别端到端测试 (黑屏 + 花屏)"
echo "========================================================"
echo "每段帧数: $SEGMENT"
echo "输出目录: $(pwd)/$OUTPUT_DIR"
echo "========================================================"
echo ""

# 1. 生成合成测试视频 (宿主机 python3, 仅需 numpy+opencv)
echo "[1/3] 生成测试视频..."
python3 scripts/gen_anomaly_video.py --output "$VIDEO_FILE" --segment "$SEGMENT"
echo ""

mkdir -p "$OUTPUT_DIR"

# 2. Docker 运行 video_processor (逐帧处理, 保证异常采样密度)
echo "[2/3] 运行 video_processor (Docker, 逐帧处理)..."
docker run --rm \
    -v "$PROJECT_DIR/data:/data/video:ro" \
    -v "$PROJECT_DIR/$OUTPUT_DIR:/data/output" \
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
    --frame_skip 1 \
    "$@"
echo ""

# 3. 校验异常 JSON
echo "[3/3] 校验异常检测结果..."
ANOMALY_JSON="$OUTPUT_DIR/anomaly_test_anomalies.json"
if [ ! -f "$ANOMALY_JSON" ]; then
    echo "FAIL: 未找到异常结果文件 $ANOMALY_JSON"
    exit 1
fi

python3 - "$ANOMALY_JSON" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
onsets = {(e["anomaly_type"], e["phase"]) for e in data if e["phase"] == "onset"}
recoveries = {(e["anomaly_type"], e["phase"]) for e in data if e["phase"] == "recovery"}
print(f"异常事件总数: {len(data)}")
for e in data:
    print(f"  {e['anomaly_type']:<13} {e['phase']:<9} @ 帧{e['frame']} ({e.get('video_second',0):.1f}s)")
ok_black = ("black_screen", "onset") in onsets
ok_flower = ("flower_screen", "onset") in onsets
print(f"\n黑屏 onset 检出: {ok_black}")
print(f"花屏 onset 检出: {ok_flower}")
if ok_black and ok_flower:
    print("\nRESULT: PASS ✅ (黑屏与花屏均正确检出)")
    sys.exit(0)
else:
    print("\nRESULT: FAIL ❌")
    sys.exit(1)
PYEOF
