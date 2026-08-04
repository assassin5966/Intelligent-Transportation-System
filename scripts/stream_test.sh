#!/bin/bash
#
# RTSP 流测试 - 启动 Docker 服务并注册设备进行实时流测试
#
# 用法:
#   ./stream_test.sh                                          # 默认: 双设备(车辆+人流)
#   ./stream_test.sh vehicle                                  # 仅车辆摄像头
#   ./stream_test.sh person                                   # 仅人流摄像头
#   ./stream_test.sh --count-only enter                       # 双设备, 只计进入
#
# 环境变量:
#   VEHICLE_VIDEO  车辆视频文件 (默认 data/车辆识别20s.mp4)
#   PERSON_VIDEO   人流视频文件 (默认 data/人流识别20s.mp4)
#   LINE           计数线 (默认 "0.1,0.75,0.9,0.75")
#   ANCHOR         内侧锚点 (默认 "0.5,0.9")
#

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

VEHICLE_VIDEO="${VEHICLE_VIDEO:-data/车辆识别20s.mp4}"
PERSON_VIDEO="${PERSON_VIDEO:-data/人流识别20s.mp4}"
LINE="${LINE:-0.1,0.75,0.9,0.75}"
ANCHOR="${ANCHOR:-0.5,0.9}"
COUNT_ONLY=""
MODE="both"

# 解析参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stop)
            echo ">>> 停止所有设备..."
            curl -s -X DELETE http://localhost:8001/devices/VEHICLE_CAM 2>/dev/null || true
            curl -s -X DELETE http://localhost:8001/devices/PERSON_CAM 2>/dev/null || true
            echo ">>> 停止所有服务..."
            docker compose down
            echo "=== 已停止 ==="
            exit 0
            ;;
        vehicle) MODE="vehicle"; shift ;;
        person)  MODE="person";  shift ;;
        --count-only) COUNT_ONLY="--count-only $2"; shift 2 ;;
        *) echo "未知参数: $1"; exit 1 ;;
    esac
done

# 构建计数线 JSON
LINE_JSON="[[0.1,0.75],[0.9,0.75]]"
ANCHOR_JSON="[0.5,0.9]"

echo "========================================================"
echo "  RTSP 流测试 (Docker)"
echo "========================================================"
echo "模式: $MODE"
echo "计数线: $LINE (水平线, 下方1/4处)"
echo "锚点: $ANCHOR (计数线下方)"
echo "========================================================"
echo ""

# 1. 启动基础服务
echo ">>> 启动 Redis + RTSP 服务器..."
docker compose up -d redis rtsp-server

# 2. 启动推流服务
if [ "$MODE" = "vehicle" ] || [ "$MODE" = "both" ]; then
    echo ">>> 启动车辆视频推流 ($VEHICLE_VIDEO)..."
    docker compose up -d rtsp-streamer-vehicle
fi

if [ "$MODE" = "person" ] || [ "$MODE" = "both" ]; then
    echo ">>> 启动人流视频推流 ($PERSON_VIDEO)..."
    docker compose up -d rtsp-streamer-person
fi

# 3. 启动 AI + 后端服务
echo ">>> 启动 AI 分析服务 + 业务后端..."
docker compose up -d ai backend

echo ">>> 等待服务启动 (10s)..."
sleep 10

# 4. 清理 Redis
echo ">>> 清理 Redis 历史数据..."
docker compose exec redis redis-cli FLUSHALL

# 5. 注册设备
if [ "$MODE" = "vehicle" ] || [ "$MODE" = "both" ]; then
    echo ""
    echo ">>> 注册车辆摄像头 (camera_type=vehicle)..."
    curl -s -X POST http://localhost:8001/devices \
        -H "Content-Type: application/json" \
        -d "{
            \"device_id\": \"VEHICLE_CAM\",
            \"stream_url\": \"rtsp://rtsp-server:8554/vehicle\",
            \"line\": $LINE_JSON,
            \"anchor\": $ANCHOR_JSON,
            \"camera_type\": \"vehicle\"
            $([ -n \"$COUNT_ONLY\" ] && echo \",\\\"count_only\\\":\\\"enter\\\"\")
        }"
    echo ""
fi

if [ "$MODE" = "person" ] || [ "$MODE" = "both" ]; then
    echo ""
    echo ">>> 注册人流摄像头 (camera_type=person)..."
    curl -s -X POST http://localhost:8001/devices \
        -H "Content-Type: application/json" \
        -d "{
            \"device_id\": \"PERSON_CAM\",
            \"stream_url\": \"rtsp://rtsp-server:8554/person\",
            \"line\": $LINE_JSON,
            \"anchor\": $ANCHOR_JSON,
            \"camera_type\": \"person\"
            $([ -n \"$COUNT_ONLY\" ] && echo \",\\\"count_only\\\":\\\"enter\\\"\")
        }"
    echo ""
fi

echo ""
echo "========================================================"
echo "  服务已启动, 等待流处理..."
echo "========================================================"
echo ""
echo "查看统计:   curl http://localhost:8000/api/stats/realtime"
echo "查看设备:   curl http://localhost:8001/devices"
echo "查看日志:   docker compose logs ai --tail 20"
echo "停止测试:   $0 --stop"
echo ""

# 等待并显示结果
echo ">>> 等待 25 秒后显示结果..."
sleep 25

echo ""
echo "=== 设备列表 ==="
curl -s http://localhost:8001/devices | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8001/devices

echo ""
echo "=== 实时统计 ==="
curl -s http://localhost:8000/api/stats/realtime | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8000/api/stats/realtime

echo ""
echo "=== AI 服务日志 (最近 15 行) ==="
docker compose logs ai --tail 15

echo ""
echo "========================================================"
echo "  测试完成"
echo "  停止服务: $0 --stop"
echo "========================================================"
