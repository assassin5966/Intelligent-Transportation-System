#!/bin/bash
# ============================================================
# Mock 模式 Docker 启动脚本
# 功能: 用 data/ 下的测试视频通过 RTSP 模拟视频流, 启动后端所有功能接口
# 架构: Redis + RTSP Server + RTSP Streamers + 后端 + AI 服务
# 用法: bash scripts/mock_start.sh [--build] [--down]
#        --build: 强制重新构建镜像
#        --down:  停止所有服务
# 停止: Ctrl+C 或 bash scripts/mock_start.sh --down
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.mock.yml"

# 日志颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---- 处理参数 ----
if [ "$1" = "--down" ]; then
    log_info "正在停止所有服务..."
    docker compose $COMPOSE_FILES down
    log_info "所有服务已停止"
    exit 0
fi

# ---- 1. 构建镜像 (可选 --build 强制重建) ----
if [ "$1" = "--build" ]; then
    log_info "构建 Docker 镜像..."
    docker compose $COMPOSE_FILES build
else
    # 检查镜像是否存在
    if ! docker images smart-city-platform:latest --format '{{.Repository}}' | grep -q 'smart-city-platform'; then
        log_info "镜像不存在, 正在构建..."
        docker compose $COMPOSE_FILES build
    else
        log_info "使用已有镜像 smart-city-platform:latest"
    fi
fi

# ---- 2. 启动所有服务 (后台模式) ----
log_info "启动 Mock 模式服务..."
docker compose $COMPOSE_FILES up -d redis rtsp-server rtsp-streamer-vehicle rtsp-streamer-person backend ai

log_info "等待服务就绪..."

# 等待后端服务就绪
BACKEND_READY=false
for i in $(seq 1 30); do
    if curl -s http://localhost:8000/health 2>/dev/null | grep -q '"ok"'; then
        BACKEND_READY=true
        log_info "后端服务已就绪 (http://localhost:8000)"
        break
    fi
    sleep 2
done

if [ "$BACKEND_READY" = false ]; then
    log_error "后端服务启动超时, 请检查日志: docker compose $COMPOSE_FILES logs backend"
    exit 1
fi

# 等待 AI 服务就绪
AI_READY=false
for i in $(seq 1 30); do
    if curl -s http://localhost:8001/health 2>/dev/null | grep -q '"ok"'; then
        AI_READY=true
        log_info "AI 服务已就绪 (http://localhost:8001)"
        break
    fi
    sleep 2
done

if [ "$AI_READY" = false ]; then
    log_error "AI 服务启动超时, 请检查日志: docker compose $COMPOSE_FILES logs ai"
    exit 1
fi

# 等待 RTSP 流就绪
log_info "等待 RTSP 视频流就绪 (约 10 秒)..."
sleep 10

# ---- 3. 注册 Mock 设备 ----
log_info "注册 Mock 设备..."

# 3a. 车辆识别摄像头 (北门) - RTSP 流
log_info "注册车辆识别摄像头..."
VEHICLE_RESP=$(curl -s -X POST "http://localhost:8000/api/devices" \
    -H "Content-Type: application/json" \
    -d '{
        "id": "mock-cam-vehicle",
        "name": "北门摄像头-车辆",
        "stream_url": "rtsp://rtsp-server:8554/vehicle",
        "line_coords": "0.3,0.5,0.7,0.5",
        "anchor_coords": "0.5,0.6",
        "count_only": "enter",
        "camera_type": "vehicle"
    }')
echo "  车辆识别: $VEHICLE_RESP"

sleep 2

# 3b. 人流识别摄像头 (南门) - RTSP 流
log_info "注册人流识别摄像头..."
PERSON_RESP=$(curl -s -X POST "http://localhost:8000/api/devices" \
    -H "Content-Type: application/json" \
    -d '{
        "id": "mock-cam-person",
        "name": "南门摄像头-人流",
        "stream_url": "rtsp://rtsp-server:8554/person",
        "line_coords": "0.5,0.3,0.5,0.7",
        "anchor_coords": "0.6,0.5",
        "camera_type": "person"
    }')
echo "  人流识别: $PERSON_RESP"

sleep 3

# ---- 4. 验证服务状态 ----
log_info "===== 验证服务状态 ====="

echo -e "\n后端健康检查:"
curl -s http://localhost:8000/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8000/health

echo -e "\nAI 服务健康检查:"
curl -s http://localhost:8001/health | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8001/health

echo -e "\n已注册设备:"
curl -s http://localhost:8000/api/devices | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8000/api/devices

echo -e "\nAI 服务运行中的管道:"
curl -s http://localhost:8001/devices | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8001/devices

# 等待几秒让 AI 服务开始拉流处理
log_info "等待 AI 服务开始拉流处理..."
sleep 5

echo -e "\nAI 服务管道状态 (应显示 running):"
curl -s http://localhost:8001/devices | python3 -m json.tool 2>/dev/null || curl -s http://localhost:8001/devices

# ---- 5. 输出访问信息 ----
echo ""
log_info "========================================"
log_info " Mock 环境启动完成!"
log_info "========================================"
echo ""
echo "  后端 API:          http://localhost:8000"
echo "  健康检查:           http://localhost:8000/health"
echo "  设备列表:           http://localhost:8000/api/devices"
echo "  事件列表:           http://localhost:8000/api/events"
echo "  告警列表:           http://localhost:8000/api/alerts"
echo "  统计信息:           http://localhost:8000/api/stats"
echo "  警力分配:           http://localhost:8000/api/police"
echo "  WebSocket:          ws://localhost:8000/ws"
echo "  AI 服务:            http://localhost:8001"
echo "  RTSP 车辆流:        rtsp://localhost:8554/vehicle"
echo "  RTSP 人流流:        rtsp://localhost:8554/person"
echo ""
echo "  注册设备:"
echo "    - mock-cam-vehicle (北门车辆, RTSP)"
echo "    - mock-cam-person  (南门人流, RTSP)"
echo ""
log_info "查看日志: docker compose $COMPOSE_FILES logs -f [service]"
log_info "停止服务: bash scripts/mock_start.sh --down"
echo ""