#!/bin/bash
# ============================================================
# Mock 模式启动脚本 (前后端一起)
# 支持两种模式:
#   1. RTSP mock (默认): data/ 下测试视频经 RTSP 模拟视频流
#      测试服务(MediaMTX/推流/video_processor)定义在 docker-compose.mock.yml
#   2. WVP  mock (--wvp): 本机 mock_wvp.py 模拟 WVP-GB28181 (端口 18080), 后端走 WVP 同步
# 前端: 后端就绪后自动以宿主机 npm run dev 启动 (http://localhost:5173),
#       同源 + vite proxy -> localhost:8000 (与开发模式一致, 无需 VITE_API_BASE)。
# 用法:
#   bash scripts/mock_start.sh [--wvp] [--build] [--test] [--down]
#     --wvp    使用 WVP mock 模式 (mock_wvp.py 模拟 WVP 设备/通道/点播)
#     --build  强制重新构建镜像
#     --test   启动完成后执行接口冒烟测试 (curl + WebSocket)
#     --down   停止所有服务 (含前端 npm run dev / mock_wvp.py)
# 停止: bash scripts/mock_start.sh --down
# ============================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

MODE="rtsp"
BUILD=false
RUN_TEST=false
DOWN=false
for arg in "$@"; do
  case "$arg" in
    --wvp)   MODE="wvp" ;;
    --build) BUILD=true ;;
    --test)  RUN_TEST=true ;;
    --down)  DOWN=true ;;
  esac
done

COMPOSE_FILES="-f docker-compose.yml -f docker-compose.mock.yml"

# 日志颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ---- 前端 (宿主机 npm run dev) ----
FRONTEND_DIR="$PROJECT_DIR/map-marking-system-vue"
FRONTEND_PORT=5173
FRONTEND_LOG=/tmp/mock_frontend.log
FRONTEND_PIDFILE=/tmp/mock_frontend.pid

# ---- Mock WVP 管理 (端口 18080) ----
MOCK_WVP_PORT=18080
MOCK_WVP_LOG=/tmp/mock_wvp.log
MOCK_WVP_PIDFILE=/tmp/mock_wvp.pid

wvp_alive() { curl -s "http://localhost:${MOCK_WVP_PORT}/api/device/query/devices" >/dev/null 2>&1; }

start_mock_wvp() {
  if wvp_alive; then
    log_info "mock_wvp 已在运行 (http://localhost:${MOCK_WVP_PORT})"
    return
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    log_error "未找到 python3, 无法启动 mock_wvp.py"
    exit 1
  fi
  log_info "启动 mock_wvp.py (模拟 WVP, 端口 ${MOCK_WVP_PORT})..."
  nohup python3 mock_wvp.py > "$MOCK_WVP_LOG" 2>&1 &
  echo $! > "$MOCK_WVP_PIDFILE"
  sleep 3
  if wvp_alive; then
    log_info "mock_wvp 就绪 (http://localhost:${MOCK_WVP_PORT})"
  else
    log_error "mock_wvp 启动失败, 日志: $MOCK_WVP_LOG"
    exit 1
  fi
}

stop_mock_wvp() {
  if [ -f "$MOCK_WVP_PIDFILE" ]; then
    kill "$(cat "$MOCK_WVP_PIDFILE")" 2>/dev/null && log_info "已停止 mock_wvp (PID $(cat "$MOCK_WVP_PIDFILE"))" || true
    rm -f "$MOCK_WVP_PIDFILE"
  fi
  pkill -f "mock_wvp.py" 2>/dev/null && log_info "已清理 mock_wvp 进程" || true
}

# ---- 前端 (宿主机 npm run dev, 同源 + vite proxy -> localhost:8000) ----
frontend_alive() { curl -s "http://localhost:${FRONTEND_PORT}" >/dev/null 2>&1; }

start_frontend() {
  if frontend_alive; then
    log_info "前端已在运行 (http://localhost:${FRONTEND_PORT})"
    return
  fi
  if ! command -v node >/dev/null 2>&1; then
    log_error "未找到 node, 无法启动前端 (npm run dev)"
    log_error "请安装 Node.js, 或改用 docker 方式启动前端"
    return
  fi
  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    log_info "安装前端依赖 (首次, npm install)..."
    (cd "$FRONTEND_DIR" && npm install --no-audit --no-fund)
  fi
  log_info "启动前端 (npm run dev, http://localhost:${FRONTEND_PORT})..."
  (cd "$FRONTEND_DIR" && nohup npm run dev > "$FRONTEND_LOG" 2>&1 & echo $! > "$FRONTEND_PIDFILE")
  local ready=false
  for i in $(seq 1 30); do
    if frontend_alive; then
      ready=true
      log_info "前端就绪 (http://localhost:${FRONTEND_PORT})"
      break
    fi
    sleep 1
  done
  if [ "$ready" = false ]; then
    log_warn "前端启动超时, 日志: $FRONTEND_LOG"
  fi
}

stop_frontend() {
  if [ -f "$FRONTEND_PIDFILE" ]; then
    kill "$(cat "$FRONTEND_PIDFILE")" 2>/dev/null || true
    rm -f "$FRONTEND_PIDFILE"
  fi
  pkill -f "npm run dev" 2>/dev/null || true
  pkill -f "map-marking-system-vue" 2>/dev/null || true
  log_info "已停止前端 (npm run dev / vite)"
}

# 宿主机在容器网络视角的网关地址 (后端容器经此访问本机 mock_wvp)
host_gateway() {
  docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || echo "172.17.0.1"
}

# 等待 MySQL 就绪 (容器内 mysqladmin ping; 首次启动需初始化, 最长 90s)
wait_mysql_ready() {
  log_info "等待 MySQL 就绪 (宿主机 localhost:3307 -> 容器内 3306)..."
  local ready=false
  for i in $(seq 1 45); do
    if docker exec dt-mysql mysqladmin ping -h127.0.0.1 -uroot -proot123456 --silent >/dev/null 2>&1; then
      ready=true
      log_info "MySQL 已就绪"
      break
    fi
    sleep 2
  done
  if [ "$ready" = false ]; then
    log_error "MySQL 启动超时, 请检查: docker compose -f docker-compose.yml logs mysql"
    exit 1
  fi
}

# ---- 接口冒烟测试 (frontend_api.md) ----
api_test() {
  local BASE="http://localhost:8000"
  local pass=0 fail=0

  check() {
    local desc="$1" code="$2" body="$3"
    local prefix="${code:0:1}"
    if [ "$prefix" = "2" ] || [ "$prefix" = "3" ]; then
      printf "  \e[32m✓\e[0m [%s] %s\n" "$code" "$desc"
      pass=$((pass+1))
    else
      printf "  \e[33m~\e[0m [%s] %s (非 2xx, 按文档预期)\n" "$code" "$desc"
      fail=$((fail+1))
    fi
    if [ -n "$body" ]; then
      echo "      body: $(echo "$body" | head -c 160)"
    fi
  }

  log_info "===== 接口冒烟测试 (curl) ====="

  local r code body
  r=$(curl -s -w "\n%{http_code}" "$BASE/health"); code=$(echo "$r"|tail -1); body=$(echo "$r"|sed '$d')
  check "GET /health" "$code" "$body"

  r=$(curl -s -w "\n%{http_code}" "$BASE/api/devices"); code=$(echo "$r"|tail -1); body=$(echo "$r"|sed '$d')
  check "GET /api/devices" "$code" "$body"

  r=$(curl -s -w "\n%{http_code}" "$BASE/api/stats/realtime"); code=$(echo "$r"|tail -1); body=$(echo "$r"|sed '$d')
  check "GET /api/stats/realtime" "$code" "$body"

  # MySQL 归档已启用 (WVP 模式默认开启), 应返回 200
  r=$(curl -s -w "\n%{http_code}" "$BASE/api/stats/hourly/history?start_date=2026-08-01:01&end_date=2026-08-22:08"); code=$(echo "$r"|tail -1); body=$(echo "$r"|sed '$d')
  check "GET /api/stats/hourly/history (MySQL 归档)" "$code" "$body"

  r=$(curl -s -w "\n%{http_code}" -X POST "$BASE/api/prediction/predict"); code=$(echo "$r"|tail -1); body=$(echo "$r"|sed '$d')
  check "POST /api/prediction/predict" "$code" "$body"

  r=$(curl -s -w "\n%{http_code}" "$BASE/api/prediction/latest"); code=$(echo "$r"|tail -1); body=$(echo "$r"|sed '$d')
  check "GET /api/prediction/latest" "$code" "$body"

  r=$(curl -s -w "\n%{http_code}" "$BASE/api/prediction/health"); code=$(echo "$r"|tail -1); body=$(echo "$r"|sed '$d')
  check "GET /api/prediction/health" "$code" "$body"

  log_info "===== WebSocket 冒烟测试 (ws://localhost:8000/ws) ====="
  python3 - <<'PYEOF'
import asyncio, json
try:
    import websockets
except ImportError:
    print("  [SKIP] 缺少 websockets 库")
    raise SystemExit(0)

async def main():
    try:
        async with websockets.connect("ws://localhost:8000/ws", open_timeout=5) as ws:
            types = []
            got_stats = False
            try:
                async with asyncio.timeout(6):
                    for _ in range(5):
                        msg = await ws.recv()
                        data = json.loads(msg)
                        types.append(data.get("type"))
                        if data.get("type") == "stats":
                            got_stats = True
            except TimeoutError:
                pass
            print(f"  [OK] 收到 {len(types)} 条消息, types={types}")
            print(f"  [OK] 包含 stats 消息: {got_stats}")
    except Exception as e:
        print(f"  [FAIL] WebSocket 连接失败: {e}")

asyncio.run(main())
PYEOF

  echo ""
  log_info "接口测试完成: PASS=$pass  NON-2XX=$fail (按文档预期, 非异常)"
}

# ============================================================
# 停止
# ============================================================
if [ "$DOWN" = true ]; then
    log_info "正在停止所有服务..."
    stop_frontend
    docker compose $COMPOSE_FILES down
    stop_mock_wvp
    log_info "所有服务已停止"
    exit 0
fi

# ============================================================
# 构建镜像
# ============================================================
if [ "$BUILD" = true ]; then
    log_info "构建 Docker 镜像..."
    docker compose $COMPOSE_FILES build
elif ! docker images smart-city-platform:latest --format '{{.Repository}}' | grep -q 'smart-city-platform'; then
    log_info "镜像不存在, 正在构建..."
    docker compose $COMPOSE_FILES build
else
    log_info "使用已有镜像 smart-city-platform:latest"
fi

# ============================================================
# WVP mock 模式 (--wvp)
# ============================================================
if [ "$MODE" = "wvp" ]; then
    log_info "===== 启动 WVP mock 模式 ====="

    # 清理旧容器
    docker compose $COMPOSE_FILES down 2>/dev/null || true

    # 启动本机 mock_wvp.py (模拟 WVP 设备/通道/点播)
    start_mock_wvp

    GW="$(host_gateway)"
    log_info "宿主机网关(容器视角): ${GW}:${MOCK_WVP_PORT}"

    # 启动 Redis + MySQL (容器内网 mysql:3306, 宿主机映射 3307 避开占用)
    log_info "启动 Redis..."
    docker compose -f docker-compose.yml up -d --no-deps redis
    log_info "启动 MySQL..."
    docker compose -f docker-compose.yml up -d --no-deps mysql
    wait_mysql_ready

    # 启动后端 + AI (WVP 同步模式, MySQL 归档开启)
    log_info "启动后端 (WVP_ENABLED=true, MYSQL_ENABLED=true)..."
    WVP_ENABLED=true \
    WVP_API_URL="http://${GW}:${MOCK_WVP_PORT}" \
    WVP_USERNAME=admin WVP_PASSWORD=admin \
    MYSQL_ENABLED=true \
    docker compose -f docker-compose.yml up -d --no-deps backend ai

    # 等待后端就绪
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
        log_error "后端服务启动超时, 请检查日志: docker compose -f docker-compose.yml logs backend"
        exit 1
    fi

    # 等待 WVP 首次同步 (每 30s 一次, 最多等 60s)
    log_info "等待 WVP 同步注册设备 (每 30s 同步一次)..."
    SYNCED=false
    for i in $(seq 1 12); do
        if curl -s http://localhost:8000/api/devices 2>/dev/null | grep -q 'GB-'; then
            SYNCED=true
            break
        fi
        sleep 5
    done
    if [ "$SYNCED" = true ]; then
        log_info "WVP 设备同步完成:"
        curl -s http://localhost:8000/api/devices | python3 -m json.tool 2>/dev/null \
          | grep -E '"id"|"name"|"status"' | head -30 || curl -s http://localhost:8000/api/devices
    else
        log_warn "60s 内未发现同步设备, 请检查: docker compose -f docker-compose.yml logs backend | grep WVP"
    fi

    # 启动前端 (宿主机 npm run dev, 同源 + vite proxy -> localhost:8000)
    start_frontend

    echo ""
    log_info "========================================"
    log_info " WVP mock 环境启动完成!"
    log_info "========================================"
    echo ""
    echo "  前端页面:           http://localhost:${FRONTEND_PORT}"
    echo "  后端 API:          http://localhost:8000"
    echo "  Mock WVP:          http://localhost:${MOCK_WVP_PORT}"
    echo "  健康检查:           http://localhost:8000/health"
    echo "  设备列表:           http://localhost:8000/api/devices"
    echo "  实时统计:           http://localhost:8000/api/stats/realtime"
    echo "  事件列表:           http://localhost:8000/api/events"
    echo "  告警列表:           http://localhost:8000/api/alerts"
    echo "  警力分配:           http://localhost:8000/api/police"
    echo "  预测:               POST http://localhost:8000/api/prediction/predict"
    echo "  WebSocket:          ws://localhost:8000/ws"
    echo "  运维工具:           http://localhost:8000/static/device-config.html"
    echo ""
    log_info "停止服务: bash scripts/mock_start.sh --down"
    echo ""

    if [ "$RUN_TEST" = true ]; then
        api_test
    fi
    exit 0
fi

# ============================================================
# RTSP mock 模式 (默认)
# ============================================================
log_info "===== 启动 RTSP mock 模式 ====="

# ---- 1. 启动所有服务 (后台模式) ----
log_info "启动 Mock 模式服务..."
docker compose $COMPOSE_FILES up -d redis mysql rtsp-server rtsp-streamer-vehicle rtsp-streamer-person backend ai

# 等待 MySQL 就绪 (MYSQL_ENABLED 默认 true, 后端归档依赖)
wait_mysql_ready

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

# ---- 2. 注册 Mock 设备 ----
log_info "注册 Mock 设备..."

# 2a. 点位1: 和阳南门出口路南以东85米 (GAJK-2648, 便道人流) - RTSP 流
log_info "注册点位1 (GAJK-2648 和阳南门出口路南以东85米)..."
PT1_RESP=$(curl -s -X POST "http://localhost:8000/api/devices" \
    -H "Content-Type: application/json" \
    -d '{
        "id": "mock-pt1-gajk2648",
        "name": "GAJK-2648 和阳南门出口路南以东85米",
        "stream_url": "rtsp://rtsp-server:8554/vehicle",
        "line_coords": "0.3,0.5,0.7,0.5",
        "anchor_coords": "0.5,0.6",
        "camera_type": "vehicle"
    }')
echo "  点位1: $PT1_RESP"

# 注册仅保存配置, 需调用 enable 启流 (WVP 同步/手动设备均通过 enable 启动管道)
sleep 1
log_info "启流点位1..."
curl -s -X POST "http://localhost:8000/api/devices/mock-pt1-gajk2648/enable" \
    -H "Content-Type: application/json" \
    -d '{"line_coords":"0.3,0.5,0.7,0.5","anchor_coords":"0.5,0.6","camera_type":"vehicle"}' | python3 -m json.tool 2>/dev/null || true

sleep 2

# 2b. 点位2: 和阳南门出口路北以东90米 (GAJK-2649, 便道人流) - RTSP 流
log_info "注册点位2 (GAJK-2649 和阳南门出口路北以东90米)..."
PT2_RESP=$(curl -s -X POST "http://localhost:8000/api/devices" \
    -H "Content-Type: application/json" \
    -d '{
        "id": "mock-pt2-gajk2649",
        "name": "GAJK-2649 和阳南门出口路北以东90米",
        "stream_url": "rtsp://rtsp-server:8554/person",
        "line_coords": "0.5,0.3,0.5,0.7",
        "anchor_coords": "0.6,0.5",
        "camera_type": "person"
    }')
echo "  点位2: $PT2_RESP"

# 注册仅保存配置, 需调用 enable 启流
sleep 1
log_info "启流点位2..."
curl -s -X POST "http://localhost:8000/api/devices/mock-pt2-gajk2649/enable" \
    -H "Content-Type: application/json" \
    -d '{"line_coords":"0.5,0.3,0.5,0.7","anchor_coords":"0.6,0.5","camera_type":"person"}' | python3 -m json.tool 2>/dev/null || true

sleep 3

# ---- 3. 验证服务状态 ----
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

# ---- 4. 输出访问信息 ----
# 启动前端 (宿主机 npm run dev, 同源 + vite proxy -> localhost:8000)
start_frontend

echo ""
log_info "========================================"
log_info " Mock 环境启动完成!"
log_info "========================================"
echo ""
echo "  前端页面:           http://localhost:${FRONTEND_PORT}"
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
echo "    - mock-pt1-gajk2648 (点位1: GAJK-2648 和阳南门出口路南以东85米)"
echo "    - mock-pt2-gajk2649 (点位2: GAJK-2649 和阳南门出口路北以东90米)"
echo ""
log_info "查看日志: docker compose $COMPOSE_FILES logs -f [service]"
log_info "停止服务: bash scripts/mock_start.sh --down"
echo ""

if [ "$RUN_TEST" = true ]; then
    api_test
fi
