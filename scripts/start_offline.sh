#!/bin/bash
# ============================================================
# 离线生产部署 一键启动 (docker-compose.offline.yml)
#
# 用途: 生产机 (23.45.1.115, 8x4090) 上镜像 load 后一键拉起
#       ai / backend / redis / frontend
# 幂等: 可重复执行, 已启动则保持运行
#
# 用法:
#   bash scripts/start_offline.sh                # 全部拉起
#   bash scripts/start_offline.sh --no-frontend  # 不起大屏前端 (无 dist 挂载时)
#
# 前置:
#   1) docker load -i <smart-city-platform 的 tar>   (镜像自包含 app/configs/models)
#   2) docker load -i redis_7-alpine.tar
#   3) docker load -i nginx_1.27-alpine.tar          (起 frontend 时)
#   4) frontend 需宿主机准备 map-marking-system-vue/{nginx.conf,dist,public/tiles}
#
# .env 完全可选: 默认已指向 23.45.1.115 的 WVP/ZLM; 镜像自动探测
#   (载入 smart-city-platform:gpu 后自动用 GPU 版并叠加 docker-compose.offline.gpu.yml)
#   仅 WVP 账号/地址与默认不同等场景才需要写 .env
# ============================================================
set -euo pipefail

START_FRONTEND=1
[ "${1:-}" = "--no-frontend" ] && START_FRONTEND=0

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.offline.yml"
COMPOSE_FILES=(-f "$COMPOSE_FILE")   # GPU 镜像时追加 offline.gpu 覆盖文件
PROJECT="smartcity"

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
ok()   { printf '\033[1;32m  ✔\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }

cd "$ROOT_DIR"

if [ ! -f "$COMPOSE_FILE" ]; then
    echo "错误: 找不到 $COMPOSE_FILE"; exit 1
fi
if ! docker info >/dev/null 2>&1; then
    echo "错误: docker 未运行"; exit 1
fi

log "======== 1/3 环境自检 ========"
# 镜像选择: .env 的 PLATFORM_IMAGE 优先; 不写则自动探测 (gpu 优先, 回退 latest)
PLATFORM_IMAGE="$(grep -E '^PLATFORM_IMAGE=' .env 2>/dev/null | cut -d= -f2 || true)"
PLATFORM_IMAGE="${PLATFORM_IMAGE:-}"
if [ -z "$PLATFORM_IMAGE" ]; then
    if docker image inspect smart-city-platform:gpu >/dev/null 2>&1; then
        PLATFORM_IMAGE=smart-city-platform:gpu
        ok "自动探测到 GPU 镜像, 使用 $PLATFORM_IMAGE (.env 无需配置)"
    else
        PLATFORM_IMAGE=smart-city-platform:latest
    fi
fi
if docker image inspect "$PLATFORM_IMAGE" >/dev/null 2>&1; then
    ok "平台镜像就绪: $PLATFORM_IMAGE"
else
    warn "缺少镜像 $PLATFORM_IMAGE, 请先: docker load -i <平台镜像tar>"; exit 1
fi
# GPU 覆盖文件 + runtime 自检 (使用 gpu 镜像时)
if [[ "$PLATFORM_IMAGE" == *gpu* ]]; then
    if [ -f "$ROOT_DIR/docker-compose.offline.gpu.yml" ]; then
        COMPOSE_FILES+=(-f "$ROOT_DIR/docker-compose.offline.gpu.yml")
        ok "已叠加 GPU 编排覆盖: docker-compose.offline.gpu.yml (8 卡预留)"
    else
        warn "缺 docker-compose.offline.gpu.yml, ai 将无 GPU 预留 (以 CPU 模式运行)"
    fi
    if docker info 2>/dev/null | grep -qi nvidia; then
        ok "nvidia-container-runtime 已就绪"
    else
        warn "未检测到 nvidia runtime, ai 容器将无法启动 (先装 nvidia-container-toolkit)"
    fi
fi
# frontend 前置检查
if [ "$START_FRONTEND" = "1" ]; then
    if [ -f "$ROOT_DIR/map-marking-system-vue/nginx.conf" ] \
       && [ -d "$ROOT_DIR/map-marking-system-vue/dist" ]; then
        ok "前端 dist/nginx.conf 就绪"
    else
        warn "缺 map-marking-system-vue/{nginx.conf,dist}, 自动跳过 frontend (--no-frontend)"
        START_FRONTEND=0
    fi
fi

log "======== 2/3 拉起容器 (compose -p $PROJECT) ========"
if [ "$START_FRONTEND" = "1" ]; then
    docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" up -d
else
    docker compose "${COMPOSE_FILES[@]}" -p "$PROJECT" up -d ai backend redis
fi
ok "容器已拉起"

log "======== 3/3 健康检查 ========"
wait_http() {  # $1=名称 $2=url $3=超时秒
    for _ in $(seq 1 "$3"); do
        if curl -sf -o /dev/null "$2"; then ok "$1 就绪 ($2)"; return 0; fi
        sleep 2
    done
    warn "$1 未在 $((3 * $3))s 内就绪, 请查日志: docker compose -f $COMPOSE_FILE -p $PROJECT logs backend"
    return 1
}
wait_http "后端 backend" "http://127.0.0.1:8000/api/devices" 30 || true
wait_http "AI 服务 ai"   "http://127.0.0.1:8001/"           30 || true
if [ "$START_FRONTEND" = "1" ]; then
    wait_http "大屏 frontend" "http://127.0.0.1:5173/"       15 || true
fi

# 访问地址 (自动探测本机 IP, 取不到用生产机默认)
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SERVER_IP="${SERVER_IP:-23.45.1.115}"
echo ""
log "======== 启动完成 ========"
echo "  后端 API : http://$SERVER_IP:8000/docs"
echo "  AI 服务  : http://$SERVER_IP:8001/"
[ "$START_FRONTEND" = "1" ] && echo "  大屏前端 : http://$SERVER_IP:5173"
echo "  日志     : docker compose -f docker-compose.offline.yml -p $PROJECT logs -f backend"
echo "  停止     : bash scripts/stop_offline.sh"
