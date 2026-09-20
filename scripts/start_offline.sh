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
#   1) docker load -i <smart-city-platform 的 tar>   (PLATFORM_IMAGE 指定的镜像)
#   2) docker load -i nginx_1.27-alpine.tar          (起 frontend 时)
#   3) 同目录 .env 可选 (WVP_ENABLED/ZLM_PUBLIC_BASE/PLATFORM_IMAGE 等, 不写走默认值)
#   4) frontend 需宿主机准备 map-marking-system-vue/{nginx.conf,dist,public/tiles}
#   5) GPU 部署: .env 写 PLATFORM_IMAGE=smart-city-platform:gpu 并取消
#      docker-compose.offline.yml 中 ai 服务 deploy 段注释 (8 卡预留)
# ============================================================
set -euo pipefail

START_FRONTEND=1
[ "${1:-}" = "--no-frontend" ] && START_FRONTEND=0

# ---------- 路径 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/docker-compose.offline.yml"
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
# 镜像检查 (PLATFORM_IMAGE 默认 smart-city-platform:latest)
PLATFORM_IMAGE="$(grep -E '^PLATFORM_IMAGE=' .env 2>/dev/null | cut -d= -f2 || true)"
PLATFORM_IMAGE="${PLATFORM_IMAGE:-smart-city-platform:latest}"
if docker image inspect "$PLATFORM_IMAGE" >/dev/null 2>&1; then
    ok "平台镜像就绪: $PLATFORM_IMAGE"
else
    warn "缺少镜像 $PLATFORM_IMAGE, 请先: docker load -i <平台镜像tar>"; exit 1
fi
# GPU 自检 (仅 GPU 镜像时提示)
if [[ "$PLATFORM_IMAGE" == *gpu* ]]; then
    if docker info 2>/dev/null | grep -q "RUNC.*nvidia\|nvidia"; then
        ok "nvidia-container-runtime 已就绪"
    else
        warn "未检测到 nvidia runtime, GPU 容器将无法启动 (检查 nvidia-container-toolkit)"
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
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" up -d
else
    docker compose -f "$COMPOSE_FILE" -p "$PROJECT" up -d ai backend redis
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
